"""lib/softsynk/core.py: paths, config, atomic files, run, lock, log, status, CLI wrapper."""
import io
import json
import os
import stat
import unittest
from contextlib import redirect_stderr
from unittest import mock

from libtest import Base, rd, wr

from softsynk import core

DEFAULTS = {"interval": 60, "ratio": 0.5, "on": True, "name": "x", "list": [],
            "nested": {"warn": 85, "crit": 95}}


class Time(Base):
    def test_clock_is_pinnable_and_round_trips(self):
        self.at("2026-11-04T10:30:00+00:00")
        t = core.now()
        self.assertEqual(core.iso(t), "2026-11-04T10:30:00+00:00")
        self.assertEqual(core.parse_iso(core.iso(t)), t)

    def test_naive_times_are_local_and_garbage_is_none(self):
        self.assertIsNotNone(core.parse_iso("2026-11-04T10:30:00").tzinfo)
        self.assertIsNone(core.parse_iso("nope"))
        self.assertIsNone(core.parse_iso(None))


class Paths(Base):
    def test_ctx_paths_follow_the_root_tree(self):
        c = core.Ctx("monitor", self.root)
        self.assertEqual(c.path("etc", "config.json"), str(self.root / "etc/monitor/config.json"))
        self.assertEqual(c.config_path, str(self.root / "etc/monitor/config.json"))
        self.assertEqual(c.lock_path, str(self.root / "lock/monitor.lock"))
        self.assertEqual(c.other("notify").path("secrets", "smtp.env"), str(self.root / "secrets/notify/smtp.env"))
        with self.assertRaises(ValueError):
            c.path("bogus")

    def test_root_defaults_to_env_then_constant(self):
        with mock.patch.dict(os.environ, {"SOFTSYNK_ROOT": str(self.root)}):
            self.assertEqual(core.Ctx("x").root, str(self.root))
        with mock.patch.dict(os.environ):
            os.environ.pop("SOFTSYNK_ROOT", None)
            self.assertEqual(core.Ctx("x").root, core.DEFAULT_ROOT)


    def test_exec_path_prefers_the_current_copy_of_this_very_file(self):
        c = core.Ctx("monitor", self.root)
        tool = self.tmp / "releases" / "1" / "deployment" / "monitor" / "synk-monitor"
        tool.parent.mkdir(parents=True)
        wr(tool, "")
        self.assertEqual(core.exec_path(c, str(tool)), str(tool))  # no current/ yet
        cur = self.root / "current"
        cur.parent.mkdir(parents=True)
        cur.symlink_to(self.tmp / "releases" / "1")
        self.assertEqual(core.exec_path(c, str(tool)), str(cur / "deployment" / "monitor" / "synk-monitor"))
        other = self.tmp / "elsewhere" / "synk-monitor"  # same name, different file: not the release copy
        other.parent.mkdir()
        wr(other, "")
        self.assertEqual(core.exec_path(c, str(other)), str(other))


class Config(Base):
    def write(self, obj):
        p = self.tmp / "config.json"
        p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
        return str(p)

    def problems(self, obj, check=None):
        with self.assertRaises(core.Fail) as cm:
            core.load_config(self.write(obj), DEFAULTS, check)
        self.assertEqual(cm.exception.code, core.USAGE)
        return str(cm.exception)

    def test_missing_file_is_none_not_an_error(self):
        self.assertIsNone(core.load_config(str(self.tmp / "nope.json"), DEFAULTS))

    def test_deep_merge_over_defaults_without_touching_them(self):
        cfg = core.load_config(self.write({"interval": 30, "nested": {"warn": 70}}), DEFAULTS)
        self.assertEqual(cfg["nested"], {"warn": 70, "crit": 95})
        self.assertEqual((cfg["interval"], cfg["name"]), (30, "x"))
        self.assertEqual(DEFAULTS["nested"], {"warn": 85, "crit": 95})
        cfg["list"].append(1)
        self.assertEqual(DEFAULTS["list"], [])

    def test_type_mismatches_unknown_keys_and_bool_vs_int_are_all_reported(self):
        msg = self.problems({"interval": "60", "on": 1, "bogus": 1, "nested": {"warn": "x", "extra": 1},
                             "list": "a"})
        for part in ("interval: expected int", "on: expected bool", "unknown key 'bogus'",
                     "nested.warn: expected int", "unknown key 'nested.extra'", "list: expected list"):
            self.assertIn(part, msg)

    def test_int_is_accepted_for_a_float_default_and_bool_is_not_an_int(self):
        self.assertEqual(core.load_config(self.write({"ratio": 1}), DEFAULTS)["ratio"], 1)
        self.assertIn("interval: expected int", self.problems({"interval": True}))

    def test_nested_value_must_be_an_object(self):
        self.assertIn("nested: expected object", self.problems({"nested": 5}))

    def test_extra_check_problems_fail_with_usage(self):
        msg = self.problems({"interval": 5}, check=lambda c: ["interval must be >= 30"] if c["interval"] < 30 else [])
        self.assertIn("interval must be >= 30", msg)

    def test_unreadable_and_non_object_files_fail_with_usage(self):
        self.assertIn("cannot read", self.problems("{not json"))
        self.assertIn("must contain a JSON object", self.problems("[1]"))


class Files(Base):
    def test_write_atomic_sets_mode_and_leaves_no_temp_file(self):
        p = str(self.tmp / "d" / "f")
        core.write_atomic(p, b"hi", 0o600)
        self.assertEqual(rd(p, "rb"), b"hi")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(os.listdir(self.tmp / "d"), ["f"])
        core.write_atomic(p, b"x", 0o640, owner=(os.getuid(), os.getgid()))  # owner is applied before the rename
        st = os.stat(p)
        self.assertEqual((st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)), (os.getuid(), os.getgid(), 0o640))

    def test_write_atomic_chowns_before_chmod_so_the_mode_bits_survive(self):
        calls = []
        with mock.patch("os.fchown", side_effect=lambda *a: calls.append("chown")), \
             mock.patch("os.fchmod", side_effect=lambda *a: calls.append("chmod")):
            core.write_atomic(str(self.tmp / "f"), b"x", 0o640, owner=(os.getuid(), os.getgid()))
        self.assertEqual(calls, ["chown", "chmod"])

    def test_write_atomic_syncs_the_directory_after_the_rename(self):
        real, seen = os.fsync, []
        def spy(fd):
            seen.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            real(fd)
        with mock.patch("os.fsync", side_effect=spy):
            core.write_atomic(str(self.tmp / "f"), b"x", 0o644)
        self.assertEqual(seen, [False, True])

    def test_failed_write_keeps_the_old_file_and_cleans_up(self):
        p = str(self.tmp / "f")
        core.write_atomic(p, b"old", 0o644)
        with mock.patch("os.fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                core.write_atomic(p, b"new", 0o644)
        self.assertEqual(rd(p, "rb"), b"old")
        self.assertEqual(os.listdir(self.tmp), ["f"])

    def test_write_if_changed_touches_nothing_when_equal(self):
        p = str(self.tmp / "f")
        self.assertTrue(core.write_if_changed(p, b"a", 0o644))
        ino = os.stat(p).st_ino
        self.assertFalse(core.write_if_changed(p, b"a", 0o644))
        self.assertEqual(os.stat(p).st_ino, ino)
        self.assertTrue(core.write_if_changed(p, b"b", 0o644))   # content differs
        self.assertTrue(core.write_if_changed(p, b"b", 0o600))   # mode differs
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_json_round_trip_default_and_corrupt(self):
        p = str(self.tmp / "s.json")
        self.assertEqual(core.read_json(p, {"d": 1}), {"d": 1})
        core.write_json(p, {"b": 1, "a": [1]})
        self.assertEqual(core.read_json(p), {"a": [1], "b": 1})
        self.assertTrue(rd(p).endswith("\n"))
        wr(p, "{broken")
        with self.assertRaises(core.Corrupt) as cm:
            core.read_json(p)
        self.assertIn(p, str(cm.exception))
        self.assertIsInstance(cm.exception, core.Fail)

    def test_env_file_is_private_round_trips_and_rejects_newlines(self):
        p = str(self.tmp / "secrets" / "x" / "smtp.env")
        self.assertEqual(core.read_env_file(p), {})
        core.write_env_file(p, {"SMTP_PASSWORD": "p=w d", "A": "b"})
        self.assertEqual(core.read_env_file(p), {"SMTP_PASSWORD": "p=w d", "A": "b"})
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode), 0o700)
        with self.assertRaises(core.Fail):
            core.write_env_file(p, {"A": "x\ny"})

    def test_env_file_tightens_an_existing_loose_parent(self):
        d = self.tmp / "secrets"
        d.mkdir(mode=0o755)
        os.chmod(d, 0o755)
        core.write_env_file(str(d / "smtp.env"), {"A": "b"})
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)


class Run(Base):
    def test_success_failure_and_input(self):
        r = core.run(["sh", "-c", "echo out; echo err >&2; exit 3"])
        self.assertEqual((r.rc, r.out.strip(), r.err.strip(), r.ok), (3, "out", "err", False))
        self.assertEqual(r.why(), "err")
        self.assertEqual(core.run(["cat"], input="piped").out, "piped")
        self.assertTrue(core.run(["true"]).ok)

    def test_missing_binary_is_rc_127_not_an_exception(self):
        r = core.run(["/nonexistent/tool", "--x"])
        self.assertEqual(r.rc, 127)
        self.assertIn("/nonexistent/tool", r.err)

    def test_timeout_kills_the_whole_process_group(self):
        marker = self.tmp / "survivor"
        r = core.run(["sh", "-c", f"(sleep 0.3; touch {marker}) & sleep 30"], timeout=0.2)
        self.assertTrue(r.timed_out)
        self.assertFalse(r.ok)
        self.assertEqual(r.why(), "timed out")
        import time
        time.sleep(0.5)
        self.assertFalse(marker.exists())


    def test_a_normal_exit_does_not_kill_what_the_command_left_running(self):
        marker = self.tmp / "daemon"
        r = core.run(["sh", "-c", f"(sleep 0.3; touch {marker}) & exit 0"])
        self.assertTrue(r.ok)
        import time
        time.sleep(0.6)
        self.assertTrue(marker.exists())


class Lock(Base):
    def test_second_holder_is_refused_and_release_frees_it(self):
        c = core.Ctx("m", self.root)
        with core.lock(c):
            with self.assertRaises(core.Busy) as cm:
                with core.lock(c):
                    self.fail("entered a held lock")
            self.assertEqual(cm.exception.code, core.FAILED)
        with core.lock(c):
            pass
        self.assertEqual(stat.S_IMODE(os.stat(c.lock_path).st_mode), 0o600)


class Log(Base):
    def lines(self, ctx):
        f = os.path.join(ctx.log, "2026-11.jsonl")
        return [json.loads(x) for x in rd(f).splitlines()]

    def test_records_are_monthly_jsonl_0640_with_run_id(self):
        self.at("2026-11-04T10:30:00+00:00")
        c = core.Ctx("m", self.root)
        core.log(c, "start", n=1)
        core.log(c, "done", ok=True)
        rows = self.lines(c)
        self.assertEqual([r["event"] for r in rows], ["start", "done"])
        self.assertEqual((rows[0]["ts"], rows[0]["run"], rows[0]["n"]), ("2026-11-04T10:30:00+00:00", c.run_id, 1))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(c.log, "2026-11.jsonl")).st_mode), 0o640)

    def test_registered_secrets_are_redacted_including_json_escaped_forms(self):
        self.at("2026-11-04T10:30:00+00:00")
        c = core.Ctx("m", self.root)
        core.register_secret('pa"ss word')
        core.register_secret("abc")  # shorter than 4 chars: not registered
        core.log(c, "auth", detail='login with pa"ss word failed abc')
        raw = rd(os.path.join(c.log, "2026-11.jsonl"))
        self.assertNotIn("ss word", raw)
        self.assertIn("abc", raw)
        self.assertEqual(core.redact("x pa\"ss word y"), "x *** y")
        self.assertEqual(core.redact("abc"), "abc")

    def test_prune_removes_only_old_monthly_files_once_per_context(self):
        self.at("2026-11-04T10:30:00+00:00")
        c = core.Ctx("m", self.root)
        os.makedirs(c.log)
        for name in ("2026-06.jsonl", "2026-07.jsonl", "2026-08.jsonl", "2026-09.jsonl", "notes.txt", "2026-06.jsonl.bak"):
            wr(os.path.join(c.log, name), "x")
        os.symlink("/etc/hosts", os.path.join(c.log, "2025-01.jsonl"))
        core.log(c, "x")
        left = sorted(os.listdir(c.log))
        # 90 days before 2026-11-04 is 2026-08-06: a month file goes only when the whole month is older
        self.assertNotIn("2026-06.jsonl", left)
        self.assertNotIn("2026-07.jsonl", left)
        for keep in ("2026-08.jsonl", "2026-09.jsonl", "notes.txt", "2026-06.jsonl.bak", "2025-01.jsonl"):
            self.assertIn(keep, left)
        wr(os.path.join(c.log, "2026-05.jsonl"), "x")
        core.log(c, "again")
        self.assertIn("2026-05.jsonl", os.listdir(c.log))  # not pruned a second time in the same run

    def test_logging_never_raises_when_the_disk_misbehaves(self):
        self.at("2026-11-04T10:30:00+00:00")
        c = core.Ctx("m", self.root)
        os.makedirs(os.path.dirname(c.log))
        wr(c.log, "a file where the log dir should be")
        core.log(c, "x")  # must not raise


class Status(unittest.TestCase):
    def test_document_shape_and_vocabulary(self):
        with mock.patch.dict(os.environ, {"SOFTSYNK_NOW": "2026-11-04T10:30:00+00:00"}):
            d = core.status_doc("monitor", "1.0.0", "installed", "degraded", "disk / 93%", warnings=["w"], actions=["a"])
        self.assertEqual(d, {"schema": 1, "module": "monitor", "version": "1.0.0", "observed_at": "2026-11-04T10:30:00Z",
                             "install_state": "installed", "health": "degraded", "summary": "disk / 93%",
                             "details": {}, "warnings": ["w"], "actions": ["a"]})
        with self.assertRaises(ValueError):
            core.status_doc("m", "1", "weird", "healthy", "")
        with self.assertRaises(ValueError):
            core.status_doc("m", "1", "installed", "fine", "")


class Cli(unittest.TestCase):
    def run_main(self, fn):
        err = io.StringIO()
        with redirect_stderr(err):
            return core.main(fn, prog="synk-x"), err.getvalue()

    def test_exit_codes(self):
        self.assertEqual(self.run_main(lambda: None)[0], 0)
        self.assertEqual(self.run_main(lambda: core.DEGRADED)[0], 1)
        code, err = self.run_main(lambda: (_ for _ in ()).throw(core.Fail("bad input", core.USAGE)))
        self.assertEqual((code, err.strip()), (64, "synk-x: bad input"))
        self.assertEqual(self.run_main(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))[0], 130)
        self.assertEqual(self.run_main(lambda: (_ for _ in ()).throw(core.Cancelled()))[0], 130)

    def test_common_args_work_before_and_after_the_subcommand_and_usage_exits_64(self):
        p = core.Parser(prog="synk-x")
        core.common_args(p)
        sub = p.add_subparsers(dest="cmd")
        core.common_args(sub.add_parser("check"))
        a = core.parse_args(p, ["--root", "/r", "check", "--json"])
        self.assertEqual((a.root, a.json, a.dry_run, a.non_interactive, a.accept_defaults), ("/r", True, False, False, False))
        a = core.parse_args(p, ["check", "--root", "/s", "--dry-run", "--accept-defaults"])
        self.assertEqual((a.root, a.dry_run, a.accept_defaults), ("/s", True, True))
        self.assertIsNone(core.parse_args(p, ["check"]).root)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            core.parse_args(p, ["--bogus"])
        self.assertEqual(cm.exception.code, 64)

    def test_common_args_json_false_rejects_the_flag(self):
        p = core.Parser(prog="synk-x")
        core.common_args(p, json=False)
        self.assertFalse(core.parse_args(p, []).json)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            core.parse_args(p, ["--json"])
        self.assertEqual(cm.exception.code, 64)

    def test_engine_dry_run_env_sets_dry_run(self):
        p = core.Parser(prog="synk-x")
        core.common_args(p)
        with mock.patch.dict(os.environ, {"SOFTSYNK_DRY_RUN": "1"}):
            self.assertTrue(core.parse_args(p, []).dry_run)
        with mock.patch.dict(os.environ, {"SOFTSYNK_DRY_RUN": "0"}):
            self.assertFalse(core.parse_args(p, []).dry_run)


if __name__ == "__main__":
    unittest.main()
