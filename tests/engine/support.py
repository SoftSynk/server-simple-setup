"""Shared helpers: build a throwaway release, run the engine, hash trees."""
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
OS_RELEASE = {k: str(FIXTURES / f"os-release.{k}") for k in ("ubuntu-24.04", "ubuntu-22.04", "debian-12")}
ZERO_SHA = "0" * 64
EOF = "\x04"  # Ctrl-D, for run_pty answers


def load_engine():
    """Import the extension-less engine script as a module (no bytecode written)."""
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader("softsynk_engine", str(REPO / "softsynk-server"))
    spec = importlib.util.spec_from_loader("softsynk_engine", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def make_release(dest, mods, version="0.1.0", registry=None):
    """Create a release dir: engine, bootstrap, VERSION, fixtures, modules.toml.

    mods: fixture directory names (also the registry ids), in registry order.
    registry: raw modules.toml text to use instead of the generated one.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("softsynk-server", "bootstrap.sh"):
        shutil.copy2(REPO / name, dest / name)
    (dest / "VERSION").write_text(version + "\n")
    lines = ["schema = 1", ""]
    for m in mods:
        target = dest / "deployment" / m
        shutil.copytree(FIXTURES / "modules" / m, target)
        if not (target / "run.sh").exists():
            shutil.copy2(FIXTURES / "modules" / "_common" / "run.sh", target / "run.sh")
        lines += ["[[modules]]", f'id = "{m}"', f'manifest = "deployment/{m}/module.toml"', ""]
    (dest / "modules.toml").write_text(registry if registry is not None else "\n".join(lines))
    return dest


def engine(release, args, root, env=None, timeout=60):
    """Run the engine non-interactively (stdin is /dev/null). Returns CompletedProcess."""
    full_env = dict(os.environ, **(env or {}))
    return subprocess.run([sys.executable, str(Path(release) / "softsynk-server"), "--root", str(root), *args],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, env=full_env,
                          timeout=timeout)


def bootstrap(release, args, env=None, timeout=60):
    return subprocess.run(["bash", str(Path(release) / "bootstrap.sh"), *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, env=dict(os.environ, **(env or {})), timeout=timeout)


def run_pty(release, args, root, answers=(), env=None, timeout=30):
    """Run the engine on a pseudo terminal, feeding answers. Returns (exit code, output)."""
    master, slave = pty.openpty()
    full_env = dict(os.environ, **(env or {}))
    p = subprocess.Popen([sys.executable, str(Path(release) / "softsynk-server"), "--root", str(root), *args],
                         stdin=slave, stdout=slave, stderr=slave, env=full_env, start_new_session=True)
    os.close(slave)
    for a in answers:
        os.write(master, a.encode() if a == EOF else (a + "\n").encode())
    out, deadline = b"", time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([master], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # EIO: the child closed its end
                break
            if not chunk:
                break
            out += chunk
        elif p.poll() is not None:
            break
    else:
        p.kill()
        raise TimeoutError(f"pty run timed out; output so far: {out.decode(errors='replace')}")
    p.wait(timeout=10)
    os.close(master)
    return p.returncode, out.decode(errors="replace")


def tree_hash(path):
    """Hash names, modes, link targets and contents of everything under path."""
    path = Path(path)
    if not os.path.lexists(path):
        return "missing"
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for n in sorted(dirnames + filenames):
            p = Path(dirpath) / n
            st = p.lstat()
            h.update(f"{p.relative_to(path)}|{st.st_mode}|".encode())
            if p.is_symlink():
                h.update(os.readlink(p).encode())
            elif p.is_file():
                h.update(p.read_bytes())
    return h.hexdigest()


def tarball(src_dir, dest, top="softsynk-release"):
    with tarfile.open(dest, "w:gz") as t:
        t.add(src_dir, arcname=top)
    return hashlib.sha256(Path(dest).read_bytes()).hexdigest()


def mark_installed(root, *ids):
    for i in ids:
        d = Path(root) / "state" / i
        d.mkdir(parents=True, exist_ok=True)
        (d / "installed").touch()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="softsynk-test-")).resolve()
        self.addCleanup(self._rm)
        self.root = self.tmp / "root"

    def _rm(self):
        subprocess.run(["chmod", "-R", "u+w", str(self.tmp)], check=False)  # releases are read-only
        shutil.rmtree(self.tmp, ignore_errors=True)

    def release(self, mods, name="rel", **kw):
        return make_release(self.tmp / name, mods, **kw)

    def status_json(self, rel, env=None):
        r = engine(rel, ["--status", "--json"], self.root, env)
        return r.returncode, json.loads(r.stdout), r
