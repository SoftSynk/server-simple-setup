"""change: backup, validate, write, reload, verify, and the way back. Runner scripted, temp dirs only."""
import hashlib
import json
import os
import signal
import stat
import unittest

from libtest import Base, rd, wr
from softsynk import change, core

V, R, X = ["check"], ["reload"], ["restart"]


class Runner:
    """Stands in for core.run: answers with the next scripted exit code (0 when the script is used up; an
    exception instance is raised). hook(n) runs first on call n (0-based) to meddle with the world."""
    def __init__(self, *rcs, hook=None, err="boom"):
        self.rcs, self.calls, self.hook, self.err = list(rcs), [], hook, err

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.hook:
            self.hook(len(self.calls) - 1)
        rc = self.rcs.pop(0) if self.rcs else 0
        if isinstance(rc, BaseException):
            raise rc
        return core.Result(rc, "", self.err if rc else "")


class ChangeBase(Base):
    def setUp(self):
        super().setUp()
        self.at("2026-11-04T12:00:00+00:00")
        self.ctx = core.Ctx("hardening", self.root)
        self.live = str(self.tmp / "etc" / "app.conf")
        core.ensure_dir(os.path.dirname(self.live))
        wr(self.live, "old\n")
        os.chmod(self.live, 0o600)

    def edit(self, text="new\n", path=None, **kw):
        return change.Edit(path or self.live, text, **kw)

    def go(self, *rcs, edits=None, run=None, **kw):
        run = run or Runner(*rcs)
        kw.setdefault("validate", V)
        kw.setdefault("reload", R)
        return change.apply(self.ctx, edits or [self.edit()], run=run, **kw), run

    def fails(self, *rcs, run=None, **kw):
        run = run or Runner(*rcs)
        kw.setdefault("validate", V)
        kw.setdefault("reload", R)
        with self.assertRaises(core.Fail) as cm:
            change.apply(self.ctx, kw.pop("edits", None) or [self.edit()], run=run, **kw)
        return str(cm.exception), run.calls

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    @property
    def backups(self):
        return self.ctx.path("data", "backups")


class Apply(ChangeBase):
    def test_unchanged_content_and_mode_is_skipped_without_side_effects(self):
        out, run = self.go(edits=[self.edit("old\n", mode=0o600), self.edit("old\n")])
        self.assertEqual(out, {"changed": [], "stamp": None})
        self.assertEqual((run.calls, self.mode(self.live), os.path.exists(self.backups)), ([], 0o600, False))

    def test_an_edit_that_only_changes_the_mode_is_a_change(self):
        out, run = self.go(edits=[self.edit("old\n", mode=0o644)])
        self.assertEqual((out["changed"], rd(self.live), self.mode(self.live), run.calls), ([self.live], "old\n", 0o644, [V, V, R]))
        self.fails(0, 1, edits=[self.edit("old\n", mode=0o600)])  # and a failed one gives the mode back
        self.assertEqual(self.mode(self.live), 0o644)

    def test_refuses_when_the_current_config_is_already_invalid(self):
        msg, calls = self.fails(1)
        self.assertIn("already", msg)
        self.assertEqual((rd(self.live), calls, os.path.exists(self.backups)), ("old\n", [V], False))

    def test_precheck_can_be_skipped_for_a_validate_that_names_a_new_file(self):
        _, run = self.go(precheck=False)
        self.assertEqual(run.calls, [V, R])

    def test_success_backs_up_validates_writes_and_reloads(self):
        out, run = self.go()
        self.assertEqual((out["changed"], out["stamp"]), ([self.live], "20261104-120000"))
        self.assertEqual((rd(self.live), self.mode(self.live), run.calls), ("new\n", 0o600, [V, V, R]))
        d = os.path.join(self.backups, out["stamp"])
        m = json.loads(rd(os.path.join(d, "manifest.json")))
        e = m["entries"][0]
        self.assertEqual((e["path"], e["existed"], e["mode"], e["sha256"]), (self.live, True, 0o600, hashlib.sha256(b"old\n").hexdigest()))
        self.assertEqual((rd(os.path.join(d, e["file"])), self.mode(d), self.mode(os.path.join(d, "manifest.json"))), ("old\n", 0o700, 0o600))
        self.assertIn('"event": "change.applied"', rd(os.path.join(self.ctx.log, "2026-11.jsonl")))

    def test_explicit_mode_applies_and_new_files_default_to_0644(self):
        fresh = str(self.tmp / "etc" / "d" / "new.conf")
        self.go(edits=[self.edit(mode=0o640), self.edit("x\n", path=fresh)])
        self.assertEqual((self.mode(self.live), self.mode(fresh), rd(fresh)), (0o640, 0o644, "x\n"))

    def test_one_argv_or_several_for_each_step(self):
        _, run = self.go(validate=[["a"], ["b"]], reload=[["r1"], ["r2"]])
        self.assertEqual(run.calls, [["a"], ["b"], ["a"], ["b"], ["r1"], ["r2"]])

    def test_validate_failure_restores_every_file_with_modes_and_does_not_reload(self):
        other = str(self.tmp / "etc" / "b.conf")
        wr(other, "b-old\n")
        os.chmod(other, 0o640)
        msg, calls = self.fails(0, 1, edits=[self.edit(mode=0o644), self.edit("b-new\n", path=other)])
        self.assertIn("restored", msg)
        self.assertEqual((rd(self.live), self.mode(self.live), rd(other), self.mode(other), calls), ("old\n", 0o600, "b-old\n", 0o640, [V, V]))

    def test_new_file_is_removed_when_the_change_is_rolled_back(self):
        fresh = str(self.tmp / "etc" / "fresh.conf")
        self.fails(0, 1, edits=[self.edit("x\n", path=fresh)])
        self.assertFalse(os.path.exists(fresh))

    def test_reload_failure_restores_and_reloads_the_old_config(self):
        msg, calls = self.fails(0, 0, 1, 0)
        self.assertEqual((rd(self.live), calls), ("old\n", [V, V, R, R]))
        self.assertIn("boom", msg)

    def test_restored_config_that_will_not_reload_falls_back_to_restart(self):
        msg, calls = self.fails(0, 0, 1, 1, 0, restart=X)
        self.assertEqual((rd(self.live), calls), ("old\n", [V, V, R, R, X]))
        self.assertIn("restart", msg)

    def test_nothing_works_after_a_rollback_says_to_fix_it_by_hand(self):
        msg, calls = self.fails(0, 0, 1, 1, 1, restart=X)
        self.assertEqual((rd(self.live), calls), ("old\n", [V, V, R, R, X]))
        self.assertIn("by hand", msg)

    def test_verify_problem_restores_and_reloads_again(self):
        msg, calls = self.fails(verify=lambda: "port 22 closed")
        self.assertEqual((rd(self.live), calls), ("old\n", [V, V, R, R]))
        self.assertIn("port 22 closed", msg)

    def test_verify_that_raises_fail_is_a_problem_too(self):
        def verify():
            raise core.Fail("cannot probe")
        msg, calls = self.fails(verify=verify)
        self.assertEqual((rd(self.live), calls[-1]), ("old\n", R))
        self.assertIn("cannot probe", msg)

    def test_symlinked_config_keeps_the_link_and_changes_the_target(self):
        link = str(self.tmp / "etc" / "enabled.conf")
        os.symlink(self.live, link)
        self.go(edits=[self.edit(path=link)])
        self.assertEqual((os.path.islink(link), rd(self.live)), (True, "new\n"))

    # ---- review findings

    def test_keyboard_interrupt_rolls_back_then_propagates(self):
        def verify():
            raise KeyboardInterrupt
        run = Runner()
        with self.assertRaises(KeyboardInterrupt):
            change.apply(self.ctx, [self.edit()], validate=V, reload=R, verify=verify, run=run)
        self.assertEqual((rd(self.live), run.calls, os.listdir(self.backups)), ("old\n", [V, V, R, R], []))

    def test_an_unexpected_error_rolls_back_then_propagates(self):
        run = Runner(0, 0, RuntimeError("runner broke"))
        with self.assertRaises(RuntimeError):
            change.apply(self.ctx, [self.edit()], validate=V, reload=R, run=run)
        self.assertEqual((rd(self.live), run.calls), ("old\n", [V, V, R, R]))

    def test_sigterm_and_sighup_roll_back_and_the_old_handlers_come_back(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(sig=sig.name):
                wr(self.live, "old\n")
                before = signal.signal(sig, lambda *a: None)  # a harmless stand-in, so a missing handler cannot kill the test run
                self.addCleanup(signal.signal, sig, before)
                stand_in = signal.getsignal(sig)
                with self.assertRaises(SystemExit) as cm:
                    change.apply(self.ctx, [self.edit()], validate=V, reload=R, verify=lambda: os.kill(os.getpid(), sig), run=Runner())
                self.assertEqual((cm.exception.code, rd(self.live), signal.getsignal(sig)), (128 + sig, "old\n", stand_in))

    def test_handlers_are_restored_after_success_and_failure_too(self):
        old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
        self.go()
        self.fails(0, 1, edits=[self.edit("other\n")])
        self.assertEqual(old, {s: signal.getsignal(s) for s in old})

    def test_a_rolled_back_apply_leaves_no_backup_behind(self):
        first = self.go()[0]["stamp"]
        self.at("2026-11-04T12:05:00+00:00")
        self.fails(0, 1, edits=[self.edit("other\n")])
        self.assertEqual((os.listdir(self.backups), change.latest(self.ctx)), ([first], first))

    def test_a_backup_that_fails_part_way_leaves_nothing_and_is_a_clean_failure(self):
        with self.assertRaises(core.Fail):
            change.backup(self.ctx, [self.live, os.path.dirname(self.live)])  # a directory cannot be copied
        self.assertEqual(os.listdir(self.backups), [])

    def test_a_directory_without_a_manifest_is_not_a_backup(self):
        os.makedirs(os.path.join(self.backups, "20261104-110000"))
        self.assertIsNone(change.latest(self.ctx))

    def test_a_partial_restore_still_reloads_and_says_so(self):
        other = str(self.tmp / "etc" / "b.conf")
        wr(other, "b-old\n")

        def tamper(n):  # during the reload: spoil the backup copy of the second file
            if n == 2:
                wr(os.path.join(self.backups, "20261104-120000", "1"), "evil\n")
        run = Runner(0, 0, 1, hook=tamper)
        msg, calls = self.fails(run=run, edits=[self.edit(), self.edit("b-new\n", path=other)])
        self.assertEqual((rd(self.live), rd(other), calls), ("old\n", "b-new\n", [V, V, R, R]))
        self.assertIn("incomplete", msg)
        self.assertIn("reloaded", msg)
        self.assertIn("20261104-120000", " ".join(os.listdir(self.backups)))  # not clean: the copies stay

    def test_restart_without_reload_is_rejected(self):
        with self.assertRaises(core.Fail):
            change.apply(self.ctx, [self.edit()], validate=V, restart=X, run=Runner())
        with self.assertRaises(core.Fail):
            change.restore(self.ctx, "20261104-120000", restart=X, run=Runner())
        self.assertEqual(rd(self.live), "old\n")

    def test_only_the_first_line_of_a_failure_reaches_the_message_and_the_log(self):
        msg, _ = self.fails(0, 1, run=Runner(0, 1, err="invalid line 3\nPASSWORD=hunter2hunter2\n"))
        self.assertIn("invalid line 3", msg)
        self.assertNotIn("hunter2", msg)
        self.assertNotIn("hunter2", rd(os.path.join(self.ctx.log, "2026-11.jsonl")))


class Restore(ChangeBase):
    def test_restore_brings_back_files_and_modes_then_reloads(self):
        out, _ = self.go(edits=[self.edit(mode=0o644)])
        run = Runner()
        r = change.restore(self.ctx, out["stamp"], reload=R, run=run)
        self.assertEqual((rd(self.live), self.mode(self.live), run.calls, r["restored"]), ("old\n", 0o600, [R], [self.live]))

    def test_restore_defaults_to_the_latest_backup(self):
        self.go()
        self.at("2026-11-04T12:05:00+00:00")
        self.go(edits=[self.edit("newer\n")])
        change.restore(self.ctx, run=Runner())
        self.assertEqual(rd(self.live), "new\n")

    def test_restore_of_a_new_file_removes_it(self):
        fresh = str(self.tmp / "etc" / "fresh.conf")
        out, _ = self.go(edits=[self.edit("x\n", path=fresh)])
        change.restore(self.ctx, out["stamp"], run=Runner())
        self.assertFalse(os.path.exists(fresh))

    def test_restore_falls_back_to_restart_and_reports_it(self):
        out, _ = self.go()
        r = change.restore(self.ctx, out["stamp"], reload=R, restart=X, run=Runner(1, 0))
        self.assertIn("restart", " ".join(r["notes"]))

    def test_tampered_backup_is_refused_and_the_live_file_left_alone(self):
        out, _ = self.go()
        d = os.path.join(self.backups, out["stamp"])
        wr(os.path.join(d, "0"), "evil\n")
        with self.assertRaises(core.Fail) as cm:
            change.restore(self.ctx, out["stamp"], run=Runner())
        self.assertIn("checksum", str(cm.exception))
        self.assertEqual(rd(self.live), "new\n")

    def test_partial_restore_still_reloads_what_could_be_restored(self):
        other = str(self.tmp / "etc" / "b.conf")
        wr(other, "b-old\n")
        out, _ = self.go(edits=[self.edit(), self.edit("b-new\n", path=other)])
        wr(os.path.join(self.backups, out["stamp"], "1"), "evil\n")
        run = Runner()
        with self.assertRaises(core.Fail) as cm:
            change.restore(self.ctx, out["stamp"], reload=R, run=run)
        self.assertEqual((rd(self.live), rd(other), run.calls), ("old\n", "b-new\n", [R]))
        self.assertIn("incomplete", str(cm.exception))

    def test_unknown_or_missing_backup_is_a_clear_failure(self):
        with self.assertRaises(core.Fail):
            change.restore(self.ctx, run=Runner())
        with self.assertRaises(core.Fail):
            change.restore(self.ctx, "../etc", run=Runner())


class Housekeeping(ChangeBase):
    def test_latest_and_prune_keep_the_newest_and_ignore_other_entries(self):
        stamps = []
        for text in ("a\n", "b\n", "c\n"):  # the clock is pinned, so the 2nd and 3rd stamps get -2 and -3
            stamps.append(self.go(edits=[self.edit(text)])[0]["stamp"])
        self.assertEqual(stamps, ["20261104-120000", "20261104-120000-2", "20261104-120000-3"])
        self.assertEqual(change.latest(self.ctx), stamps[2])
        os.mkdir(os.path.join(self.backups, "notes"))
        os.symlink(self.tmp, os.path.join(self.backups, "20200101-000000"))
        change.prune(self.ctx, keep=2)
        self.assertEqual(sorted(os.listdir(self.backups)), ["20200101-000000", stamps[1], stamps[2], "notes"])

    def test_latest_without_backups_is_none(self):
        self.assertIsNone(change.latest(self.ctx))


if __name__ == "__main__":
    unittest.main()
