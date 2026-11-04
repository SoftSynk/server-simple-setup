"""Unit tests for synk-backup: our own decisions, with restic, pg_restore and psql stubbed. No PostgreSQL, no restic."""
import argparse
import contextlib
import datetime as dt
import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402

sb = helpers.load_module()


def setUpModule():
    os.environ["TZ"] = "UTC"
    time.tzset()


def snap(target, tier, when, kind="db", sid="a" * 64):
    tags = [sb.MANAGED, f"kind={kind}", f"tier={tier}"] + ([f"db={target}"] if kind == "db" else [])
    return sb.Snap({"id": sid, "time": when, "tags": tags})


class Periods(unittest.TestCase):
    def test_period_boundaries(self):
        D = dt.datetime
        # twice monthly: 1-14 and 15-end
        self.assertEqual(sb.period_key("twice_monthly", D(2026, 1, 14, 23, 59)), (2026, 1, 1))
        self.assertEqual(sb.period_key("twice_monthly", D(2026, 1, 15, 0, 0)), (2026, 1, 2))
        self.assertEqual(sb.period_key("twice_monthly", D(2026, 1, 31)), (2026, 1, 2))
        self.assertEqual(sb.period_key("twice_monthly", D(2026, 2, 1)), (2026, 2, 1))
        # ISO weeks: Monday starts a week, and 2025-12-29 already belongs to 2026-W01
        self.assertNotEqual(sb.period_key("weekly", D(2026, 1, 4)), sb.period_key("weekly", D(2026, 1, 5)))
        self.assertEqual(sb.period_key("weekly", D(2025, 12, 29)), sb.period_key("weekly", D(2026, 1, 1)))
        self.assertEqual(sb.period_key("daily", D(2026, 1, 1, 23, 59)), sb.period_key("daily", D(2026, 1, 1, 0, 0)))
        self.assertNotEqual(sb.period_key("daily", D(2026, 1, 1, 23, 59)), sb.period_key("daily", D(2026, 1, 2, 0, 0)))

    def test_due_tiers_per_target(self):
        now = dt.datetime(2026, 1, 15, 6, 0)
        snaps = [snap("a", "daily", "2026-01-15T00:10:00+00:00"),
                 snap("a", "weekly", "2026-01-12T00:10:00+00:00"),
                 snap("a", "twice_monthly", "2026-01-14T00:10:00+00:00"),
                 snap("b", "daily", "2026-01-14T00:10:00+00:00")]
        self.assertEqual(sb.due_tiers(snaps, "a", now), ["twice_monthly"])
        self.assertEqual(sb.due_tiers(snaps, "b", now), ["daily", "weekly", "twice_monthly"])
        self.assertEqual(sb.due_tiers([], "a", now), list(sb.TIERS))

    def test_plan_tiers_globals_follow_the_databases(self):
        now = dt.datetime(2026, 1, 15, 6, 0)
        full = lambda n: [snap(n, "daily", "2026-01-15T00:10:00+00:00"), snap(n, "weekly", "2026-01-12T00:10:00+00:00"),
                          snap(n, "twice_monthly", "2026-01-15T00:10:00+00:00")]
        g = lambda tier: snap("x", tier, "2026-01-15T00:10:00+00:00", kind="globals")
        names = ["a", "b", sb.GLOBALS]
        # everything is current: nothing to do, also for globals
        snaps = full("a") + full("b") + [g("daily"), g("twice_monthly"), snap("x", "weekly", "2026-01-12T00:10:00+00:00", kind="globals")]
        self.assertEqual(sb.plan_tiers(snaps, names, now), {"a": [], "b": [], sb.GLOBALS: []})
        # a database tier is due while globals already have it this period: globals are refreshed (R8)
        snaps = [x for x in full("a") if x.tier != "daily"] + full("b") + [g("daily"), g("twice_monthly"),
                                                                           snap("x", "weekly", "2026-01-12T00:10:00+00:00", kind="globals")]
        self.assertEqual(sb.plan_tiers(snaps, names, now), {"a": ["daily"], "b": [], sb.GLOBALS: ["daily"]})
        # union over databases, in tier order; globals also keep their own catch-up
        snaps = [snap("a", "daily", "2026-01-14T00:10:00+00:00"), snap("b", "weekly", "2026-01-05T00:10:00+00:00"),
                 snap("a", "weekly", "2026-01-12T00:10:00+00:00"), snap("b", "daily", "2026-01-15T00:10:00+00:00"),
                 snap("a", "twice_monthly", "2026-01-15T00:10:00+00:00"), snap("b", "twice_monthly", "2026-01-15T00:10:00+00:00"),
                 g("daily"), g("twice_monthly"), snap("x", "weekly", "2026-01-12T00:10:00+00:00", kind="globals")]
        self.assertEqual(sb.plan_tiers(snaps, names, now),
                         {"a": ["daily"], "b": ["weekly"], sb.GLOBALS: ["daily", "weekly"]})
        snaps = full("a") + full("b") + [g("daily"), g("twice_monthly")]  # globals miss only the weekly one
        self.assertEqual(sb.plan_tiers(snaps, names, now)[sb.GLOBALS], ["weekly"])
        # --db runs have no globals target
        self.assertEqual(sb.plan_tiers([], ["a"], now), {"a": list(sb.TIERS)})

    def test_globals_duplicates_keep_newest_per_tier_and_period(self):
        g = lambda tier, when, sid: snap("x", tier, when, kind="globals", sid=sid)
        snaps = [g("daily", "2026-01-15T06:00:00+00:00", "d1"), g("daily", "2026-01-15T12:00:00+00:00", "d2"),
                 g("daily", "2026-01-15T09:00:00+00:00", "d3"), g("daily", "2026-01-14T06:00:00+00:00", "d0"),
                 g("twice_monthly", "2026-01-15T06:00:00+00:00", "t1"), g("twice_monthly", "2026-01-20T06:00:00+00:00", "t2"),
                 g("twice_monthly", "2026-01-02T06:00:00+00:00", "t0"),
                 snap("app", "daily", "2026-01-15T06:00:00+00:00", sid="db1"),
                 snap("app", "daily", "2026-01-15T12:00:00+00:00", sid="db2")]
        self.assertEqual(sorted(sb.globals_duplicates(snaps)), ["d1", "d3", "t1"])
        self.assertEqual(sb.globals_duplicates([]), [])

    def test_snapshot_time_parsing(self):
        self.assertEqual(snap("a", "daily", "2026-01-01T06:15:00.123456789+02:00").time, dt.datetime(2026, 1, 1, 4, 15, 0, 123456))
        self.assertEqual(snap("a", "daily", "2026-01-01T06:15:00Z").time, dt.datetime(2026, 1, 1, 6, 15))
        s = snap("x", "weekly", "2026-01-01T06:15:00+00:00", kind="globals")
        self.assertEqual((s.target, s.tier), (sb.GLOBALS, "weekly"))


class Names(unittest.TestCase):
    def test_allowlist(self):
        for ok in ("app", "a", "My_DB-1.x", "_x", "9lives", "a" * 63):
            self.assertTrue(sb.DB_RE.match(ok), ok)
        for bad in ("", "-x", ".x", "a b", "a;b", "a'b", 'a"b', "a\nb", "a/b", "a\\b", "a" * 64, "ü", "a$b", "a`b", "a=b"):
            self.assertFalse(sb.DB_RE.match(bad), repr(bad))

    def test_conninfo_escaping(self):
        self.assertEqual(sb.conninfo("app"), "dbname='app'")
        self.assertEqual(sb.conninfo("a'b"), "dbname='a\\'b'")
        self.assertEqual(sb.conninfo("a\\b"), "dbname='a\\\\b'")


class ConfigValidation(unittest.TestCase):
    def cfg(self, **over):
        c = sb._merge(sb.DEFAULTS, {"local_repo": "/x/local"})
        c = sb._merge(c, over)
        return c

    def test_defaults_valid(self):
        self.assertEqual(sb.validate_cfg(self.cfg()), [])

    def test_rejects(self):
        cases = [
            {"retention": {"daily": 0}}, {"retention": {"weekly": "3"}}, {"retention": {"twice_monthly": 400}},
            {"compression": "zip"}, {"mail_to": "a b@x"}, {"mail_to": "a@x,b@y"}, {"local_repo": "rel/path"},
            {"exclude_databases": ["ok", "bad name"]}, {"pg": {"host": "-evil"}}, {"pg": {"port": "x"}},
            {"pg": {"runas": "a b"}}, {"pg": {"bin_dir": "relative"}}, {"timeouts": {"dump": 0}},
            {"remote": {"preset": "custom", "endpoint": "http://example.com", "bucket": "bkt", "region": "r", "prefix": ""}},
            {"remote": {"preset": "custom", "endpoint": "https://e.example.com/path", "bucket": "bkt", "region": "r", "prefix": ""}},
            {"remote": {"preset": "custom", "endpoint": "https://e.example.com", "bucket": "B_ad", "region": "r", "prefix": ""}},
            {"remote": {"preset": "custom", "endpoint": "https://e.example.com", "bucket": "bkt", "region": "r", "prefix": "../x"}},
            {"remote": {"repo": "-o evil"}},
            {"remote": {"repo": "rest:https://user:pw@host:8000/repo"}},
            {"remote": {"repo": "rest:https://user@host:8000/repo"}},
            {"remote": {"repo": "rest:http://:pw@127.0.0.1:8000/"}},
        ]
        for over in cases:
            self.assertTrue(sb.validate_cfg(self.cfg(**over)), over)

    def test_accepts_loopback_http_and_prefix(self):
        r = {"preset": "custom", "endpoint": "http://127.0.0.1:9000", "bucket": "bkt", "region": "us-east-1", "prefix": "a/b-c"}
        self.assertEqual(sb.validate_cfg(self.cfg(remote=r)), [])
        self.assertEqual(sb.remote_spec(self.cfg(remote=r)), "s3:http://127.0.0.1:9000/bkt/a/b-c")

    def test_repo_url_credentials_point_to_the_environment(self):
        p = sb.validate_cfg(self.cfg(remote={"repo": "rest:https://user:pw@host:8000/repo"}))
        self.assertEqual(len(p), 1)
        self.assertIn("RESTIC_REST_USERNAME", p[0])
        self.assertIn("RESTIC_REST_PASSWORD", p[0])
        self.assertNotIn("pw@", p[0])  # the message never echoes the credentials
        for ok in ("rest:https://host:8000/repo", "rest:https://host/a@b", "sftp:backup@host:/srv/restic",
                   "/mnt/backup/repo", "s3:https://s3.example.com/bkt"):
            self.assertEqual(sb.validate_cfg(self.cfg(remote={"repo": ok})), [], ok)

    def test_presets(self):
        self.assertEqual(sb.PRESETS["r2"][0].format(id="abc"), "https://abc.r2.cloudflarestorage.com")
        self.assertEqual(sb.PRESETS["b2"][0].format(id="us-west-004"), "https://s3.us-west-004.backblazeb2.com")
        self.assertEqual(sb.PRESETS["hetzner"][0].format(id="fsn1"), "https://fsn1.your-objectstorage.com")
        for ep, region in sb.PRESETS.values():
            self.assertIsNone(sb.check_endpoint(ep.format(id="abc1")))


class Processes(unittest.TestCase):
    def test_timeout_kills_whole_group(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        pidfile = os.path.join(d, "pid")
        t0 = time.time()
        r = sb.run_cmd(["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], timeout=1)
        self.assertTrue(r.timed_out)
        self.assertLess(time.time() - t0, 15)
        with open(pidfile) as f:
            pid = int(f.read())
        time.sleep(0.3)
        self.assertFalse(helpers.pid_alive(pid), "background child survived the timeout")

    def test_stderr_tail_and_missing_binary(self):
        r = sb.run_cmd(["sh", "-c", "echo oops >&2; exit 3"])
        self.assertEqual((r.rc, r.ok, r.why()), (3, False, "oops"))
        self.assertEqual(sb.run_cmd(["/nonexistent/tool"]).rc, 127)

    def test_redact(self):
        sb._SECRET_VALUES.add("hunter2-secret")
        self.addCleanup(sb._SECRET_VALUES.discard, "hunter2-secret")
        self.assertEqual(sb.redact("pw=hunter2-secret!"), "pw=***!")


class Scratch(unittest.TestCase):
    """A configured scratch root (config and password only) for commands that run without PostgreSQL or restic."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.ctx = sb.Ctx(os.path.join(self.d, "root"))
        os.makedirs(self.ctx.etc)
        os.makedirs(self.ctx.sec_dir)
        with open(self.ctx.config_path, "w") as f:
            json.dump({"schema": 1, "local_repo": os.path.join(self.d, "repo"), "pg": {"runas": ""}}, f)
        with open(self.ctx.pass_path, "w") as f:
            f.write("scratch-password\n")
        sb.need_config(self.ctx)


class Redaction(Scratch):
    SECRET = "s3cr3t-value-123"

    def setUp(self):
        super().setUp()
        sb._SECRET_VALUES.add(self.SECRET)
        self.addCleanup(sb._SECRET_VALUES.discard, self.SECRET)

    def test_check_error_is_redacted(self):
        state = {"targets": {}}
        with mock.patch.object(sb, "restic", lambda *a, **k: sb.Res(1, "", f"Fatal: key {self.SECRET} rejected")):
            self.assertFalse(sb.do_check(self.ctx, state, dt.datetime(2026, 1, 1)))
        self.assertIn("rejected", state["check"]["error"])
        self.assertNotIn(self.SECRET, json.dumps(state))

    def test_restore_test_error_is_redacted(self):
        state = {"targets": {}}
        snaps = [snap("app", "daily", "2026-01-01T00:00:00+00:00")]
        with mock.patch.object(sb, "restic", lambda *a, **k: sb.Res(1, "", f"Fatal: {self.SECRET} unreadable")):
            self.assertFalse(sb.do_restore_test(self.ctx, state, dt.datetime(2026, 1, 1), snaps))
        self.assertIn("unreadable", state["restore_test"]["error"])
        self.assertNotIn(self.SECRET, json.dumps(state))


class MailToDeprecated(Scratch):
    def warnings(self, mail_to):
        out = []
        ok = mock.Mock(ok=True, out="170000", rc=0)
        version = lambda ctx, tool, runas=False: "restic 0.17.1" if tool == "restic" else "pg 17.0"  # noqa: E731
        with mock.patch.multiple(sb, tool_version=version, psql=lambda *a, **k: ok, discover=lambda ctx: [],
                                 restic=lambda *a, **k: mock.Mock(rc=0)):
            sb.validate_all(self.ctx, dict(self.ctx.cfg, mail_to=mail_to), lambda lvl, name, msg: out.append((lvl, name, msg)))
        return [(n, m) for lvl, n, m in out if lvl == "warn"]

    def test_set_mail_to_warns_and_points_at_notify(self):
        (name, msg), = self.warnings("ops@example.com")
        self.assertEqual(name, "mail")
        self.assertIn("synk-notify", msg)

    def test_empty_mail_to_is_silent(self):
        self.assertEqual(self.warnings(""), [])


class RestoreToFile(Scratch):
    def args(self, dest):
        return argparse.Namespace(db="app", globals=False, to_db=None, to_file=dest, replace_existing=False,
                                  snapshot="latest", source="local", confirm_restore=None)

    def run_restore(self, dump):
        dest = os.path.join(self.d, "out.dump")
        snaps = [snap("app", "daily", "2026-01-01T00:00:00+00:00")]
        with mock.patch.object(sb, "list_snaps", lambda *a, **k: snaps), mock.patch.object(sb, "restic", dump), \
                mock.patch.object(sb, "locked", lambda ctx: None), mock.patch.object(sb, "say", lambda m="": None):  # the real lock fd lives until process exit
            try:
                return dest, sb.cmd_restore(self.ctx, self.args(dest)), None
            except BaseException as e:  # noqa: B036 (Interrupted is a BaseException)
                return dest, None, e

    def test_partial_file_removed_on_interrupt(self):
        def dump(ctx, spec, args, stdout=None, **kw):
            os.write(stdout, b"half a dump")
            raise sb.Interrupted(15)
        dest, rc, exc = self.run_restore(dump)
        self.assertIsInstance(exc, sb.Interrupted)
        self.assertFalse(os.path.exists(dest), "partial restore file left behind")

    def test_partial_file_removed_on_failure_and_kept_on_success(self):
        def bad(ctx, spec, args, stdout=None, **kw):
            os.write(stdout, b"half a dump")
            return sb.Res(1, "", "boom")
        dest, rc, exc = self.run_restore(bad)
        self.assertIsInstance(exc, sb.Fail)
        self.assertFalse(os.path.exists(dest))

        def good(ctx, spec, args, stdout=None, **kw):
            os.write(stdout, b"whole dump")
            return sb.Res(0)
        dest, rc, exc = self.run_restore(good)
        self.assertEqual((rc, exc), (sb.OK, None))
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"whole dump")
        self.assertEqual(oct(os.stat(dest).st_mode & 0o777), "0o600")


class PgVersion(unittest.TestCase):
    def test_major_from_version_lines(self):
        for line, major in (("pg_dump (PostgreSQL) 18.0 (Ubuntu 18.0-1.pgdg24.04+1)", 18),
                            ("pg_dump (PostgreSQL) 16.4 (Homebrew)", 16),
                            ("pg_dump (PostgreSQL) 17.2", 17),
                            ("pg_dump (PostgreSQL) 15beta1", 15),
                            ("psql (PostgreSQL) 14.13 (Debian 14.13-1.pgdg120+1)", 14)):
            self.assertEqual(sb.pg_major(line), major, line)
        for junk in (None, "", "pg_dump: command not found", "pg_dump (PostgreSQL)"):
            self.assertIsNone(sb.pg_major(junk), junk)


class OldDatabaseName(unittest.TestCase):
    def test_fits_identifier_limit_and_allowlist(self):
        t = dt.datetime(2026, 1, 2, 3, 4, 5)
        for db in ("app", "My_DB-1.x", "a" * 25, "a" * 63, "9lives"):
            n = sb.old_db_name(db, t)
            self.assertTrue(sb.DB_RE.match(n), n)
            self.assertLessEqual(len(n), 63)
            self.assertTrue(n.startswith("softsynk_restore_old_" + db[:25]), n)
            self.assertTrue(n.endswith("_20260102T030405Z"), n)
        self.assertEqual(sb.old_db_name("app", t), "softsynk_restore_old_app_20260102T030405Z")

    def test_restore_prefix_is_never_backed_up(self):
        self.assertTrue(sb.old_db_name("app", dt.datetime(2026, 1, 2)).startswith(sb.RESTORE_PREFIX))


class Files(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_write_if_changed(self):
        p = os.path.join(self.d, "sub", "f")
        self.assertTrue(sb.write_if_changed(p, b"x", 0o600))
        st = os.stat(p)
        time.sleep(0.01)
        self.assertFalse(sb.write_if_changed(p, b"x", 0o600))
        self.assertEqual(os.stat(p).st_mtime_ns, st.st_mtime_ns)
        self.assertTrue(sb.write_if_changed(p, b"y", 0o600))
        self.assertTrue(sb.write_if_changed(p, b"y", 0o644))
        self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o644")

    def test_cleanup_logs_guards(self):
        ctx = sb.Ctx(self.d)
        old = os.path.join(ctx.log_root, "25", "08", "db-backup")
        new = os.path.join(ctx.log_root, "26", "01", "db-backup")
        os.makedirs(old)
        os.makedirs(new)
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        files = {
            "old": os.path.join(old, "20250801T061500-abcd1234.jsonl"),
            "recent": os.path.join(new, "20260101T061500-abcd1234.jsonl"),
            "foreign": os.path.join(old, "notes.txt"),
            "outside": os.path.join(outside, "20200101T000000-abcd1234.jsonl"),
        }
        for p in files.values():
            open(p, "w").close()
        os.symlink(files["outside"], os.path.join(old, "20200101T000000-deadbeef.jsonl"))  # symlinked file
        os.makedirs(os.path.join(ctx.log_root, "26", "02"))
        os.symlink(outside, os.path.join(ctx.log_root, "26", "02", "db-backup"))  # symlinked dir
        sb.cleanup_logs(ctx, dt.datetime(2026, 1, 10))
        self.assertFalse(os.path.exists(files["old"]))
        self.assertTrue(os.path.exists(files["recent"]))
        self.assertTrue(os.path.exists(files["foreign"]))  # not ours by name
        self.assertTrue(os.path.exists(files["outside"]))  # outside the tree, reached via symlinks
        self.assertTrue(os.path.islink(os.path.join(old, "20200101T000000-deadbeef.jsonl")))
        os.unlink(files["foreign"])
        os.unlink(os.path.join(old, "20200101T000000-deadbeef.jsonl"))
        sb.cleanup_logs(ctx, dt.datetime(2026, 1, 10))
        self.assertFalse(os.path.exists(os.path.join(ctx.log_root, "25")), "empty directories are removed")
        self.assertTrue(os.path.isdir(ctx.log_root))


class Status(unittest.TestCase):
    """Health matrix from hand-written config and state (no restic, no PostgreSQL)."""
    NOW = "2026-01-10T12:00:00"

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.ctx = sb.Ctx(self.d)
        self.write_cfg()
        self.state = self.good_state()
        os.environ["SYNK_BACKUP_NOW"] = self.NOW
        self.addCleanup(os.environ.pop, "SYNK_BACKUP_NOW", None)

    def write_cfg(self, remote=True):
        cfg = {"schema": 1, "local_repo": os.path.join(self.d, "repo"),
               "remote": {"preset": "custom", "repo": os.path.join(self.d, "remote")} if remote else None}
        os.makedirs(self.ctx.etc)
        with open(self.ctx.config_path, "w") as f:
            json.dump(cfg, f)

    def good_state(self):
        tiers = {"daily": {"last_success": "2026-01-10T06:15:00"}, "weekly": {"last_success": "2026-01-08T06:15:00"},
                 "twice_monthly": {"last_success": "2026-01-01T06:15:00"}}
        return {"schema": 1,
                "last_attempt": {"id": "x", "started": "2026-01-10T06:15:00", "finished": "2026-01-10T06:30:00", "result": "success"},
                "last_success": "2026-01-10T06:30:00",
                "targets": {"app": {"first_seen": "2026-01-01T06:15:00", "present": True, "status": "ok", "tiers": tiers},
                            "_globals": {"first_seen": "2026-01-01T06:15:00", "present": True, "status": "ok", "tiers": tiers}},
                "remote": {"ok": True, "last_copy": "2026-01-10T06:20:00", "error": ""},
                "check": {"last": "2026-01-08T06:00:00", "ok": True, "error": ""},
                "restore_test": {"last": "2026-01-02T06:00:00", "ok": True, "last_success": "2026-01-02T06:00:00"},
                "prune": {}}

    def doc(self):
        os.makedirs(os.path.dirname(self.ctx.state_path), exist_ok=True)
        with open(self.ctx.state_path, "w") as f:
            json.dump(self.state, f)
        d = sb.build_status(sb.Ctx(self.d))
        d["details"].pop("schedule", None)
        return d

    def assertHealth(self, want, d=None, contains=""):
        d = d or self.doc()
        self.assertEqual(d["health"], want, d["summary"] + str(d["warnings"]))
        self.assertIn(contains, " ".join(d["warnings"] + [d["summary"]]))

    def test_healthy(self):
        d = self.doc()
        if d["health"] == "degraded":  # a real systemd timer may be inactive on the machine running the tests
            self.assertIn("timer", " ".join(d["warnings"]))
        else:
            self.assertHealth("healthy", d)
        self.assertEqual(d["schema"], 1)
        self.assertEqual(d["module"], "backup")
        self.assertEqual(d["install_state"], "installed")

    def test_not_installed_and_unknown(self):
        shutil.rmtree(self.ctx.etc)
        d = sb.build_status(sb.Ctx(self.d))
        self.assertEqual((d["install_state"], d["health"]), ("not_installed", "unknown"))
        self.write_cfg()
        d = sb.build_status(sb.Ctx(self.d))  # configured, never ran
        self.assertEqual((d["install_state"], d["health"]), ("installed", "unknown"))

    def test_degraded_cases(self):
        self.state["remote"] = {"ok": False, "error": "net down"}
        self.assertHealth("degraded", contains="remote copy failing")
        self.state = self.good_state()
        self.state["restore_test"] = {"last": "2025-11-01T00:00:00", "ok": True, "last_success": "2025-11-01T00:00:00"}
        self.assertHealth("degraded", contains="restore test is old")
        self.state = self.good_state()
        self.state["restore_test"] = {}
        self.assertHealth("degraded", contains="never passed")
        self.state = self.good_state()
        shutil.rmtree(self.ctx.etc)
        self.write_cfg(remote=False)
        self.assertHealth("degraded", contains="no off-server remote")

    def test_unhealthy_cases(self):
        self.state["targets"]["app"].update(status="failed", last_error="pg_dump failed")
        self.assertHealth("unhealthy", contains="pg_dump failed")
        self.state = self.good_state()
        self.state["targets"]["app"]["tiers"]["daily"]["last_success"] = "2026-01-07T06:15:00"
        self.assertHealth("unhealthy", contains="overdue: app/daily")
        self.state = self.good_state()
        self.state["check"] = {"last": "2026-01-10T00:00:00", "ok": False, "error": "pack damaged"}
        self.assertHealth("unhealthy", contains="integrity check failed")
        self.state = self.good_state()
        self.state["restore_test"] = {"last": "2026-01-10T00:00:00", "ok": False, "error": "mismatch", "last_success": "2026-01-02T06:00:00"}
        self.assertHealth("unhealthy", contains="restore test failed")
        self.state = self.good_state()
        self.state["last_attempt"] = {"id": "x", "started": "2026-01-09T12:00:00", "result": "in_progress"}
        self.assertHealth("unhealthy", contains="interrupted")

    def test_overdue_uses_first_seen_and_ignores_removed_targets(self):
        self.state["targets"]["new"] = {"first_seen": "2026-01-07T00:00:00", "present": True, "tiers": {}}
        self.assertHealth("unhealthy", contains="overdue: new/daily")
        self.state = self.good_state()
        self.state["targets"]["gone"] = {"first_seen": "2025-01-01T00:00:00", "present": False, "tiers": {}}
        d = self.doc()
        self.assertNotIn("gone", " ".join(d["warnings"]))

    def test_stale_takes_precedence(self):
        self.state["last_attempt"].update(started="2026-01-08T06:15:00", finished="2026-01-08T06:30:00")
        d = self.doc()
        self.assertEqual(d["health"], "stale")
        self.assertEqual(sb.HEALTH_EXIT[d["health"]], 3)

    def test_running_is_not_a_problem(self):
        self.state["last_attempt"] = {"id": "x", "started": "2026-01-10T11:50:00", "result": "in_progress"}
        d = self.doc()
        self.assertTrue(d["details"].get("running"))
        self.assertNotEqual(d["health"], "unhealthy")


class Retention(unittest.TestCase):
    def test_reconcile_rebuilds_from_snapshots(self):
        t = dt.datetime(2026, 1, 10)
        state = {"targets": {"gone": {"tiers": {"daily": {}}}, "kept": {"last_error": "x", "tiers": {}}}}
        snaps = [snap("a", "daily", "2026-01-09T01:00:00+00:00"), snap("a", "daily", "2026-01-10T01:00:00+00:00"),
                 snap("x", "weekly", "2026-01-05T01:00:00+00:00", kind="globals")]
        sb.reconcile(state, snaps, t)
        self.assertEqual(state["targets"]["a"]["tiers"]["daily"]["last_success"], "2026-01-10T01:00:00")
        self.assertEqual(state["targets"][sb.GLOBALS]["tiers"]["weekly"]["last_success"], "2026-01-05T01:00:00")
        self.assertNotIn("gone", state["targets"])
        self.assertIn("kept", state["targets"])  # failing targets stay visible


class FakeRestic:
    """In-memory restic repositories keyed by repository spec: only the commands and options synk-backup uses.
    A required option we forgot to pass (or an unknown command) fails the test instead of being guessed."""

    def __init__(self):
        self.repos, self.calls, self.prunes, self.n = {}, [], [], 0
        self.clock = None          # the simulated "now", stamped on prunes
        self.fail_copy = False
        self.fail_tags = set()     # a backup carrying one of these tags fails ("db=app1", "kind=globals")

    def new_id(self):
        self.n += 1
        return f"{self.n:064x}"

    def __call__(self, ctx, spec, args, ro=False, stdin=None, stdout=None, timeout=None, extra_env=None):
        self.calls.append((spec, list(args)))
        snaps = self.repos.setdefault(spec, [])
        cmd = args[0]
        opt = lambda k: args[args.index(k) + 1]  # noqa: E731 (ValueError = option missing)
        if cmd in ("unlock", "check"):
            return sb.Res(0)
        if cmd == "snapshots":
            return sb.Res(0, json.dumps(snaps))
        if cmd == "backup":
            tags = opt("--tag").split(",")
            self.assertHost(opt("--host"))
            if self.fail_tags & set(tags) or not stdin.read():
                return sb.Res(1, "", "boom")
            sid = self.new_id()
            snaps.append({"id": sid, "time": opt("--time").replace(" ", "T") + "+00:00", "tags": tags})
            return sb.Res(0, json.dumps({"message_type": "summary", "snapshot_id": sid}))
        if cmd == "copy":  # a copy gets new snapshot ids; what the destination already has is skipped
            src, _ = opt("--from-repo"), opt("--from-password-file")
            if self.fail_copy:
                return sb.Res(1, "", "network down")
            key = lambda s: (s["time"], tuple(sorted(s["tags"])))  # noqa: E731
            have = {key(s) for s in snaps}
            snaps += [dict(s, id=self.new_id()) for s in self.repos.get(src, []) if key(s) not in have]
            return sb.Res(0)
        if cmd == "forget" and "--keep-last" not in args:
            gone = set(args[1:])
            snaps[:] = [s for s in snaps if s["id"] not in gone]
            return sb.Res(0)
        if cmd == "forget":  # the policy: per tag set, keep the newest N of what carries all the --tag tags
            assert opt("--group-by") == "tags"
            self.assertHost(opt("--host"))
            want, groups, out = set(opt("--tag").split(",")), {}, []
            for s in sorted((x for x in snaps if want <= set(x["tags"])), key=lambda x: x["time"], reverse=True):
                groups.setdefault(tuple(sorted(s["tags"])), []).append(s)
            for g in groups.values():
                gone = {s["id"] for s in g[int(opt("--keep-last")):]}
                snaps[:] = [s for s in snaps if s["id"] not in gone]
                out.append({"remove": sorted(gone) or None})
            return sb.Res(0, json.dumps(out))
        if cmd == "prune":
            self.prunes.append((spec, self.clock, list(args)))
            return sb.Res(0)
        raise AssertionError(f"unexpected restic call: {args}")

    @staticmethod
    def assertHost(host):
        assert host == sb.HOST_TAG, host  # retention groups must never depend on the machine name

    def backups(self, tag=None):
        return sum(1 for _, a in self.calls if a[0] == "backup" and (tag is None or tag in a[a.index("--tag") + 1].split(",")))


class Simulation(Scratch):
    """Run and retention decisions over simulated days: injected clock, in-memory restic, stubbed dumps."""
    START = dt.datetime(2026, 1, 1, 6, 15)  # a Thursday, ISO week 1
    LOCAL, REMOTE = "local", "remote-repo"
    NAMES = ("app1", "app2", sb.GLOBALS)

    def setUp(self):
        super().setUp()
        self.ctx.cfg["remote"] = {"repo": self.REMOTE}
        self.ctx.cfg["local_repo"] = self.LOCAL
        self.fake, self.dbs, self.dump_fails, self.restore_tests = FakeRestic(), [("app1", 100), ("app2", 10)], set(), []
        self.state = sb.load_state(self.ctx)

        def dump_to(ctx, tool, args, path, timeout):
            if os.path.basename(path).split(".")[0] in self.dump_fails:
                return sb.Res(1, "", "pg_dump: boom"), 0
            data = b"-- PostgreSQL database cluster dump\n" if tool == "pg_dumpall" else b"PGDMP"
            with open(path, "wb") as f:
                f.write(data)
            return sb.Res(0), len(data)

        def restore_test(ctx, state, t, snaps, **kw):
            self.restore_tests.append(t)
            state["restore_test"] = {"last": sb.iso(t), "ok": True, "error": ""}
            return True

        for name, fn in {"restic": self.fake, "discover": lambda ctx: list(self.dbs), "dump_to": dump_to, "say": lambda m="": None,
                         "toc_tables": lambda ctx, path: (2, None), "preflight_space": lambda *a: None,
                         "pg_version_line": lambda ctx: "pg_dump (PostgreSQL) 17.0", "do_restore_test": restore_test}.items():
            p = mock.patch.object(sb, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def run_at(self, t):
        self.fake.clock, self.ctx.remote_down = t, False  # every run is a new process
        stage = sb.new_stage(self.ctx)
        try:
            out = sb.do_run(self.ctx, self.state, stage, argparse.Namespace(db=None), t)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        self.state = json.loads(json.dumps(self.state))  # what the next process would read back
        return out

    def day(self, n, hours=0):
        return self.START + dt.timedelta(days=n, hours=hours)

    def kept(self, repo):
        """{(target, tier): ["MM-DD", ...]} of the snapshots a repository holds."""
        out = {}
        for s in map(sb.Snap, self.fake.repos[repo]):
            out.setdefault((s.target, s.tier), []).append(s.time.strftime("%m-%d"))
        return {k: sorted(v) for k, v in out.items()}

    def test_thirty_days_leave_three_daily_one_weekly_two_twice_monthly(self):
        for n in range(30):
            self.assertEqual(self.run_at(self.day(n)), ("success", ""), n)
            before, ncalls = self.fake.backups(), len(self.fake.calls)
            self.assertEqual(self.run_at(self.day(n, 12)), ("success", ""), n)
            self.assertEqual(self.fake.backups(), before, "a second run on the same day must not snapshot again")
            again = {a[0] for _, a in self.fake.calls[ncalls:]}
            self.assertFalse(again & {"backup", "forget", "prune"}, f"day {n}: {again}")
        want = {(name, tier): dates for name in self.NAMES for tier, dates in
                {"daily": ["01-28", "01-29", "01-30"], "weekly": ["01-26"], "twice_monthly": ["01-01", "01-15"]}.items()}
        self.assertEqual(self.kept(self.LOCAL), want)
        self.assertEqual(self.kept(self.REMOTE), want)
        # one prune per repository per run at most, and the remote at most weekly
        self.assertTrue(all(p[2] == ["prune", "--max-unused", "5%"] for p in self.fake.prunes))
        stamps = [(spec, clock) for spec, clock, _ in self.fake.prunes]
        self.assertEqual(len(stamps), len(set(stamps)))
        remote = [c for spec, c, _ in self.fake.prunes if spec == self.REMOTE]
        self.assertGreaterEqual(len(remote), 3)
        self.assertTrue(all(b - a >= dt.timedelta(days=7) for a, b in zip(remote, remote[1:])), remote)
        # weekly integrity check, monthly restore test
        self.assertEqual(sum(1 for spec, a in self.fake.calls if a[0] == "check" and spec == self.LOCAL), 5)
        self.assertEqual(len(self.restore_tests), 1)

    def test_new_database_refreshes_globals_without_duplicates(self):
        self.run_at(self.day(0))
        self.dbs.append(("app3", 5))
        self.assertEqual(self.run_at(self.day(2)), ("success", ""))  # Saturday: same week and half-month as day 0
        kept = self.kept(self.LOCAL)
        # app3 needs every tier, so the globals were saved again for all of them (R8) ...
        self.assertEqual(self.fake.backups("kind=globals"), 3 + 3)
        self.assertEqual({t for (n, t), _ in kept.items() if n == "app3"}, set(sb.TIERS))
        # ... and the superseded ones are gone: one globals snapshot per tier and period, in both repositories
        self.assertEqual(kept[(sb.GLOBALS, "weekly")], ["01-03"])
        self.assertEqual(kept[(sb.GLOBALS, "twice_monthly")], ["01-03"])
        self.assertEqual(kept[(sb.GLOBALS, "daily")], ["01-01", "01-03"])
        self.assertEqual(self.kept(self.REMOTE), kept)

    def test_failed_database_does_not_stop_the_others_and_heals_next_run(self):
        self.run_at(self.day(0))
        self.dump_fails.add("app1")
        result, error = self.run_at(self.day(1))
        self.assertEqual(result, "partial")
        self.assertIn("app1", error)
        self.assertEqual(self.state["targets"]["app1"]["status"], "failed")
        kept = self.kept(self.LOCAL)
        self.assertEqual(kept[("app1", "daily")], ["01-01"])
        self.assertEqual(kept[("app2", "daily")], ["01-01", "01-02"])
        self.assertEqual(kept[(sb.GLOBALS, "daily")], ["01-01", "01-02"])
        self.dump_fails.add("app2"), self.dump_fails.add(sb.GLOBALS)
        self.assertEqual(self.run_at(self.day(2))[0], "failed")  # nothing at all succeeded
        self.dump_fails.clear()
        self.assertEqual(self.run_at(self.day(3)), ("success", ""))
        self.assertEqual(self.kept(self.LOCAL)[("app1", "daily")], ["01-01", "01-04"])
        self.assertEqual((self.state["targets"]["app1"]["status"], self.state["targets"]["app1"]["attempts"]), ("ok", 0))

    def test_remote_failure_skips_remote_housekeeping_and_is_retried(self):
        for n in range(5):
            self.run_at(self.day(n))
        self.fake.fail_copy = True
        ncalls = len(self.fake.calls)
        result, error = self.run_at(self.day(5))
        self.assertEqual(result, "partial")
        self.assertIn("remote", error)
        self.assertTrue(self.ctx.remote_down)
        self.assertFalse(self.state["remote"]["ok"])
        self.assertTrue(self.state["retention_failed"], "the remote must be retried next run")
        remote_cmds = {a[0] for spec, a in self.fake.calls[ncalls:] if spec == self.REMOTE}
        self.assertEqual(remote_cmds, {"unlock", "copy"}, "no forget or prune against a remote that just failed")
        self.fake.fail_copy = False
        self.assertEqual(self.run_at(self.day(6)), ("success", ""))
        self.assertFalse(self.state["retention_failed"])
        self.assertEqual(self.kept(self.REMOTE), self.kept(self.LOCAL))

    def test_changed_retention_is_applied_on_a_run_that_creates_nothing(self):
        for n in range(3):
            self.run_at(self.day(n))
        self.assertEqual(self.kept(self.LOCAL)[("app1", "daily")], ["01-01", "01-02", "01-03"])
        self.ctx.cfg["retention"]["daily"] = 1
        before = self.fake.backups()
        self.assertEqual(self.run_at(self.day(2, 12)), ("success", ""))
        self.assertEqual(self.fake.backups(), before)
        for repo in (self.LOCAL, self.REMOTE):
            self.assertEqual(self.kept(repo)[("app1", "daily")], ["01-03"], repo)
        self.assertEqual(self.state["retention_applied"], self.ctx.cfg["retention"])


class ReplaceExisting(Scratch):
    """Restore sequencing against a fake psql and pg_restore: what is run, and in which order."""

    def setUp(self):
        super().setUp()
        self.log, self.said = [], []
        self.owner, self.connections, self.restore, self.fail_sql = "app_owner", "0", sb.Res(0), None
        self.dump = os.path.join(self.d, "app1.dump")
        open(self.dump, "wb").close()
        self.snap = snap("app1", "daily", "2026-01-01T00:00:00+00:00")

        def psql(ctx, sql, db="postgres", **kw):
            self.log.append(sql)
            if self.fail_sql and sql.startswith(self.fail_sql):
                return sb.Res(1, "", "boom")
            if "pg_stat_activity" in sql:
                return sb.Res(0, self.connections + "\n")
            if "pg_get_userbyid" in sql:
                return sb.Res(0, self.owner + "\n")
            if sql.startswith("SELECT count(*) FROM pg_class"):
                return sb.Res(0, "2\n")
            return sb.Res(0)

        def run_cmd(argv, **kw):
            self.log.append(shlex.join(argv))
            return self.restore

        for name, fn in {"psql": psql, "run_cmd": run_cmd, "say": self.said.append}.items():
            p = mock.patch.object(sb, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def index(self, text):
        return next(i for i, e in enumerate(self.log) if text in e)

    def replace(self):
        return sb.replace_existing(self.ctx, "app1", self.dump, 2, self.snap)

    def test_restores_as_the_original_owner_then_swaps_and_keeps_the_old_database(self):
        self.assertEqual(self.replace(), sb.OK)
        (create,) = [e for e in self.log if e.startswith("CREATE DATABASE")]
        tmp = re.fullmatch(r'CREATE DATABASE "(softsynk_restore_tmp_[0-9a-f]{8})" TEMPLATE template0 OWNER "app_owner"', create).group(1)
        restore = self.log[self.index("pg_restore")]
        self.assertNotIn("--no-owner", restore)
        self.assertIn("--exit-on-error", restore)
        self.assertIn(f"-d {tmp}", restore)
        old = re.search(r'ALTER DATABASE "app1" RENAME TO "(softsynk_restore_old_app1_\d{8}T\d{6}Z)"', "\n".join(self.log)).group(1)
        order = [self.index("CREATE DATABASE"), self.index("pg_restore"), self.index(f'"app1" RENAME TO "{old}"'),
                 self.index(f'"{tmp}" RENAME TO "app1"')]
        self.assertEqual(order, sorted(order), self.log)
        self.assertFalse([e for e in self.log if "DROP DATABASE" in e], "the replaced database must be kept")
        (drop,) = [m for m in self.said if "DROP DATABASE" in m]
        self.assertEqual(shlex.split(drop), ["psql", "-d", "postgres", "-c", f'DROP DATABASE "{old}"'])
        self.assertTrue(any(old in m and "kept" in m for m in self.said))

    def test_role_missing_in_the_dump_fails_before_the_swap_and_removes_the_copy(self):
        self.restore = sb.Res(1, "", 'pg_restore: error: role "bob" does not exist')
        with self.assertRaises(sb.Fail) as cm:
            self.replace()
        self.assertIn("restore the globals first", str(cm.exception))
        self.assertFalse([e for e in self.log if "RENAME" in e], "nothing may be renamed")
        self.assertTrue([e for e in self.log if e.startswith('DROP DATABASE IF EXISTS "softsynk_restore_tmp_')])

    def test_owner_name_is_quoted(self):
        self.owner = 'we"ird'
        self.replace()
        self.assertIn('OWNER "we""ird"', next(e for e in self.log if e.startswith("CREATE DATABASE")))

    def test_refuses_active_connections_and_a_missing_database(self):
        self.connections = "2"
        with self.assertRaises(sb.Fail) as cm:
            self.replace()
        self.assertEqual(cm.exception.code, sb.USAGE)
        self.assertEqual(len(self.log), 1, "nothing but the connection check may run")
        self.connections, self.owner, self.log[:] = "0", "", []
        with self.assertRaises(sb.Fail) as cm:
            self.replace()
        self.assertEqual(cm.exception.code, sb.USAGE)
        self.assertIn("--to-db", str(cm.exception))
        self.assertFalse([e for e in self.log if e.startswith(("CREATE", "ALTER", "DROP"))])

    def test_failed_rename_restores_the_name_and_drops_the_copy(self):
        self.fail_sql = 'ALTER DATABASE "softsynk_restore_tmp_'
        with self.assertRaises(sb.Fail) as cm:
            self.replace()
        self.assertIn("original kept", str(cm.exception))
        self.assertTrue([e for e in self.log if re.fullmatch(r'ALTER DATABASE "softsynk_restore_old_app1_\w+" RENAME TO "app1"', e)])
        self.assertTrue([e for e in self.log if e.startswith('DROP DATABASE IF EXISTS "softsynk_restore_tmp_')])

    def test_scratch_restore_keeps_no_owner_and_checks_the_table_count(self):
        sb.restore_into(self.ctx, self.dump, "scratch", 2)
        self.assertNotIn("OWNER", next(e for e in self.log if e.startswith("CREATE DATABASE")))
        self.assertIn("--no-owner", self.log[self.index("pg_restore")])
        with self.assertRaises(sb.Fail) as cm:
            sb.restore_into(self.ctx, self.dump, "scratch2", 3)
        self.assertIn("restored 2 tables, backup lists 3", str(cm.exception))
        self.assertTrue([e for e in self.log if e == 'DROP DATABASE IF EXISTS "scratch2" WITH (FORCE)'])


class StatusExit(unittest.TestCase):
    """--status exits by health (json or not); --status-json (the module contract) always exits 0."""

    def doc(self, health, install_state="installed"):
        return {"schema": 1, "module": "backup", "install_state": install_state, "health": health, "summary": "s",
                "warnings": [], "details": {"local": {"location": "/x", "free_bytes": None}, "remote": None, "databases": {}}}

    def main(self, doc, *argv):
        out = io.StringIO()
        with mock.patch.object(sb, "build_status", lambda ctx: doc), mock.patch.object(sb.signal, "signal"), \
                contextlib.redirect_stdout(out):
            return sb.main(["--root", "/nonexistent", *argv]), out.getvalue()

    def test_status_exits_by_health_with_or_without_json(self):
        want = {"healthy": 0, "degraded": 1, "unhealthy": 2, "unknown": 3, "stale": 3}
        for health, code in want.items():
            for argv in (["--status"], ["--status", "--json"]):
                rc, out = self.main(self.doc(health), *argv)
                self.assertEqual(rc, code, (health, argv))
                if "--json" in argv:
                    self.assertEqual(json.loads(out)["health"], health)

    def test_status_json_flag_always_exits_zero(self):
        for health in ("healthy", "degraded", "unhealthy", "unknown", "stale"):
            rc, out = self.main(self.doc(health), "--status-json")
            self.assertEqual((rc, json.loads(out)["health"]), (0, health))

    def test_not_installed_is_unknown_for_status_only(self):
        doc = self.doc("healthy", "not_installed")
        self.assertEqual(self.main(doc, "--status", "--json")[0], 3)
        self.assertEqual(self.main(doc, "--status")[0], 3)
        self.assertEqual(self.main(doc, "--status-json")[0], 0)


class Targets(Scratch):
    NOW = "2026-01-10T06:15:00"

    def setUp(self):
        super().setUp()
        self.said = []
        os.environ["SYNK_BACKUP_NOW"] = self.NOW
        self.addCleanup(os.environ.pop, "SYNK_BACKUP_NOW", None)
        for name, fn in {"say": self.said.append}.items():
            p = mock.patch.object(sb, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def test_reserved_excluded_and_unsafe_names_are_never_backed_up(self):
        found = [("app1", 1), ("app2", 1), ("softsynk_restore_tmp_ab12cd34", 1),
                 ("softsynk_restore_old_app1_20260101T000000Z", 1), ("bad name", 1), ("-dash", 1)]
        self.ctx.cfg["exclude_databases"] = ["app2"]
        with mock.patch.object(sb, "discover", lambda ctx: found):
            targets, bad = sb.select_targets(self.ctx, argparse.Namespace(db=None))
            self.assertEqual(targets, [("app1", 1)])
            self.assertEqual(list(bad), [ascii("bad name"), ascii("-dash")])
            for name in ("softsynk_restore_tmp_ab12cd34", "app2"):  # skipped means not found, even when named
                with self.assertRaises(sb.Fail) as cm:
                    sb.select_targets(self.ctx, argparse.Namespace(db=name))
                self.assertEqual(cm.exception.code, sb.USAGE)

    def test_dry_run_plans_what_the_run_would_including_globals(self):
        snaps = [snap(n, tier, "2026-01-10T01:00:00+00:00", kind="globals" if n == sb.GLOBALS else "db", sid=f"{i}{j}".ljust(64, "0"))
                 for i, n in enumerate(("app1", sb.GLOBALS)) for j, tier in enumerate(sb.TIERS)]
        with mock.patch.object(sb, "list_snaps", lambda ctx, spec: snaps), \
                mock.patch.object(sb, "discover", lambda ctx: [("app1", 10), ("app2", 20)]):
            self.assertEqual(sb.run_dry(self.ctx, argparse.Namespace(db=None)), sb.OK)
            line = {m.split(":")[0].strip(): m for m in self.said if m.startswith("  ")}
            self.assertIn("up to date", line["app1"])
            self.assertIn("snapshot tiers daily, weekly, twice_monthly", line["app2"])
            # globals are complete themselves but app2 is new, so they follow it (R8)
            self.assertIn("snapshot tiers daily, weekly, twice_monthly", line[sb.GLOBALS])
            self.said.clear()
            sb.run_dry(self.ctx, argparse.Namespace(db="app1"))
            self.assertFalse([m for m in self.said if sb.GLOBALS in m], "a single-database run skips globals")


if __name__ == "__main__":
    unittest.main()
