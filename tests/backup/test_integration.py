"""One real smoke test: a throwaway PostgreSQL cluster (random loopback port) and real restic repositories.

Everything about our own decisions (tiers, retention, status codes, restore sequencing, redaction, ...) is
covered by the stubbed tests in test_unit.py. This file only proves the glue: the real tools accept the
arguments we build, end to end: configure -> run one database -> restore it three ways.
It needs restic and the PostgreSQL server binaries and takes about half a minute (restic key derivation).

    python3 -B -m unittest tests.backup.test_integration
"""
import os
import shlex
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402

NOW = "2026-01-01T06:15:00"


@unittest.skipUnless(helpers.tools_available(), "needs restic and PostgreSQL server binaries")
class Smoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = helpers.PgCluster()
        cls.pg.start()
        cls.addClassCleanup(cls.pg.stop)

    def cli(self, r, *args, rc=0):
        x = r.cli(*args, now=NOW)
        self.assertEqual(x.returncode, rc, f"{args}: rc={x.returncode}\n{x.stdout}\n{x.stderr}")
        return x

    def test_configure_run_one_database_and_restore(self):
        pg = self.pg
        pg.sql("postgres", "CREATE ROLE smoke_owner")
        self.addCleanup(pg.sql, "postgres", "DROP ROLE IF EXISTS smoke_owner")
        pg.create_db("app1", rows=50, tables=2)
        pg.sql("postgres", "ALTER DATABASE app1 OWNER TO smoke_owner")
        pg.sql("app1", "ALTER TABLE t0 OWNER TO smoke_owner")
        self.addCleanup(pg.reset)
        r = helpers.Root(pg)
        self.addCleanup(r.cleanup)

        x = r.configure()
        self.assertEqual(x.returncode, 0, x.stdout + x.stderr)

        # run one database: every tier on the local repository, then the copy to the second repository
        self.assertIn("run success", self.cli(r, "run", "--db", "app1").stdout)
        for src in ("local", "remote"):
            tiers = sorted(s["tier"] for s in r.snapshots(src) if s["db"] == "app1")
            self.assertEqual(tiers, ["daily", "twice_monthly", "weekly"], src)

        # restore into a new database from each repository
        pg.sql("app1", "INSERT INTO t0 VALUES (9999, 'later')")
        self.cli(r, "restore", "--db", "app1", "--to-db", "app1_local")
        self.cli(r, "restore", "--db", "app1", "--from", "remote", "--to-db", "app1_remote")
        for db in ("app1_local", "app1_remote"):
            self.assertEqual(pg.sql(db, "SELECT count(*) FROM t0"), "50", db)
            self.assertEqual(pg.sql(db, "SELECT v FROM t0 WHERE id = 7"), pg.sql("app1", "SELECT v FROM t0 WHERE id = 7"))

        # restore to a file: mode 0600 and readable by pg_restore
        f = os.path.join(r.tmp, "out.dump")
        self.cli(r, "restore", "--db", "app1", "--to-file", f)
        self.assertEqual(oct(os.stat(f).st_mode & 0o777), "0o600")
        lst = subprocess.run([pg.tool("pg_restore"), "--list", f], capture_output=True, text=True)
        self.assertIn("TABLE DATA public t0", lst.stdout)

        # replace the live database: owners survive, the old one is kept and the printed DROP command works
        pg.sql("app1", "DELETE FROM t0")
        x = self.cli(r, "restore", "--db", "app1", "--replace-existing", "--confirm-restore=app1")
        self.assertEqual(pg.sql("app1", "SELECT count(*) FROM t0"), "50")
        self.assertEqual(pg.sql("postgres", "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'app1'"),
                         "smoke_owner")
        self.assertEqual(pg.sql("app1", "SELECT tableowner FROM pg_tables WHERE tablename = 't0'"), "smoke_owner")
        (old,) = [d for d in pg.user_dbs() if d.startswith("softsynk_restore_old_app1_")]
        self.assertEqual(pg.sql(old, "SELECT count(*) FROM t0"), "0", "the kept database is the one that was replaced")
        (line,) = [ln for ln in x.stdout.splitlines() if "DROP DATABASE" in ln]
        subprocess.run(shlex.split(line), check=True, capture_output=True, env=dict(os.environ, **pg.env()))
        self.assertNotIn(old, pg.user_dbs())


if __name__ == "__main__":
    unittest.main()
