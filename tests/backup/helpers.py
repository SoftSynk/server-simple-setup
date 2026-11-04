"""Shared fixtures for the backup tests: module loader, throwaway PostgreSQL cluster, scratch management root."""
import atexit
import glob
import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SYNK = os.path.realpath(os.path.join(HERE, "..", "..", "deployment", "backup", "synk-backup"))
PASSWORD = "correct-horse-battery-staple"


def load_module():
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader("synk_backup", SYNK)
    spec = importlib.util.spec_from_loader("synk_backup", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def find_pg_bin():
    cands = [os.environ.get("SYNK_TEST_PG_BIN", "")]
    cands += sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin"), reverse=True)
    cands += sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    w = shutil.which("initdb")
    cands.append(os.path.dirname(w) if w else "")
    for c in cands:
        if c and os.path.exists(os.path.join(c, "initdb")) and os.path.exists(os.path.join(c, "pg_dump")):
            return c
    return None


def tools_available():
    return bool(find_pg_bin() and shutil.which("restic"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PgCluster:
    """initdb in a temp dir, TCP on a random loopback port, no unix socket, trust auth."""

    def __init__(self):
        self.bin = find_pg_bin()
        self.dir = tempfile.mkdtemp(prefix="synk-pg-")
        self.data = os.path.join(self.dir, "data")
        self.port = free_port()
        self.started = False
        atexit.register(self.stop)

    def tool(self, name):
        return os.path.join(self.bin, name)

    def start(self):
        subprocess.run([self.tool("initdb"), "-D", self.data, "-U", "postgres", "--auth=trust", "-E", "UTF8",
                        "--no-locale"], check=True, capture_output=True)
        with open(os.path.join(self.data, "postgresql.conf"), "a") as f:
            f.write(f"\nlisten_addresses='127.0.0.1'\nport={self.port}\nunix_socket_directories=''\n"
                    "fsync=off\nshared_buffers=16MB\nmax_connections=40\n")
        subprocess.run([self.tool("pg_ctl"), "-D", self.data, "-l", os.path.join(self.dir, "pg.log"), "-w",
                        "start"], check=True, capture_output=True)
        self.started = True

    def stop(self):
        if self.started:
            subprocess.run([self.tool("pg_ctl"), "-D", self.data, "-m", "immediate", "stop"], capture_output=True)
            self.started = False
        shutil.rmtree(self.dir, ignore_errors=True)

    def env(self):
        return {"PGUSER": "postgres", "PGCONNECT_TIMEOUT": "10"}

    def sql(self, db, sql, check=True):
        r = subprocess.run([self.tool("psql"), "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1",
                            "-p", str(self.port), "-d", db, "-c", sql], capture_output=True, text=True,
                           env=dict(os.environ, **self.env()))
        if check and r.returncode:
            raise AssertionError(f"psql failed: {r.stderr}")
        return r.stdout.strip()

    def user_dbs(self):
        out = self.sql("postgres", "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'")
        return [x for x in out.split("\n") if x]

    def reset(self):
        for d in self.user_dbs():
            self.sql("postgres", 'DROP DATABASE "%s" WITH (FORCE)' % d.replace('"', '""'))

    def create_db(self, name, rows=50, tables=2):
        self.sql("postgres", 'CREATE DATABASE "%s"' % name.replace('"', '""'))
        if rows:
            for i in range(tables):
                self.sql(name, f"CREATE TABLE t{i} (id int primary key, v text); "
                               f"INSERT INTO t{i} SELECT g, md5(g::text) FROM generate_series(1, {rows}) g")


class Root:
    """A scratch management root plus helpers to drive the real CLI against a PgCluster."""

    def __init__(self, pg):
        self.pg = pg
        self.tmp = tempfile.mkdtemp(prefix="synk-root-")
        self.root = os.path.join(self.tmp, "root")
        self.remote = os.path.join(self.tmp, "remote-repo")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, now=None):
        e = dict(os.environ, TZ="UTC", **self.pg.env())
        e.pop("SYNK_BACKUP_NOW", None)
        if now:
            e["SYNK_BACKUP_NOW"] = now
        return e

    def cli(self, *args, now=None, input=None, timeout=600):
        return subprocess.run([sys.executable, SYNK, "--root", self.root, *args], capture_output=True, text=True,
                              input=input, env=self.env(now), timeout=timeout)

    def configure(self):
        args = ["configure", "--pg-runas", "", "--pg-bin-dir", self.pg.bin, "--pg-host", "127.0.0.1",
                "--pg-port", str(self.pg.port), "--secrets-stdin", "--remote-repo", self.remote]
        return self.cli(*args, input=json.dumps({"restic_password": PASSWORD}))

    def snapshots(self, source="local"):
        r = self.cli("snapshots", "--from", source, "--json")
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
