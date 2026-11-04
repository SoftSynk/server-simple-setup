"""History files (daily JSONL: pacing, retention, size cap, corrupt lines) and state files (atomic, /run fallback)."""
import errno
import json
import os
import types

from montest import Base, MBase, T0, at, cfgof, core, ml, mock, statvfs_for, timedelta, unittest

DAY = "2026-11-04"


def rec(minutes=0, **kw):
    r = {"t": core.iso(at(minutes)), "lvl": "ok", "cpu": 10.0, "mem": 60.0, "swap": 0.0, "load": [0.1, 0.1, 0.1],
         "disk": {"/": 70.0}, "ino": {"/": 5.0}, "rx": 100, "tx": 50, "failed": 0, "down": []}
    r.update(kw)
    return r


def roomy(mount):  # 100 GB free
    return types.SimpleNamespace(f_bavail=100 << 20, f_frsize=1 << 10, f_blocks=1, f_bfree=1, f_files=1, f_ffree=1)


class Files(Base):
    def setUp(self):
        super().setUp()
        self.h = str(self.tmp / "history")
        self.cfg = cfgof()["history"]
        os.makedirs(self.h)

    def day(self, name, size=None, text=None):
        p = os.path.join(self.h, name + ".jsonl")
        with open(p, "w") as f:
            f.write(text or "")
            if size:
                f.truncate(size)
        return p

    def append(self, r=None, **cfg):
        return ml.append_history(self.h, r or rec(), dict(self.cfg, **cfg), at(0), statvfs=roomy)

    def names(self):
        return sorted(os.listdir(self.h))

    def test_append_writes_one_json_line_into_the_file_of_that_day(self):
        self.assertIsNone(self.append())
        self.assertIsNone(self.append(rec(5)))
        lines = open(os.path.join(self.h, DAY + ".jsonl")).read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["cpu"], 10.0)
        self.assertLess(len(lines[0]), 400)

    def test_a_torn_last_line_does_not_swallow_the_next_record(self):
        self.day(DAY, text='{"t": "2026-11-04T09:00:00+00:00", "cpu": 1')  # crash mid-write, no newline
        self.assertIsNone(self.append())
        got, bad = ml.read_history(self.h, T0 - timedelta(days=1), at(1))
        self.assertEqual((len(got), bad), (1, 1))

    def test_retention_deletes_whole_days_older_than_the_limit_by_file_name(self):
        for n in ("2026-10-20", "2026-10-27", "2026-10-28", "2026-11-03"):
            self.day(n, text="{}\n")
        for n in ("notes.txt", "2026-10-01.jsonl.bak", "latest.json"):
            open(os.path.join(self.h, n), "w").write("keep")
        self.append()
        self.assertEqual(self.names(), ["2026-10-01.jsonl.bak", "2026-10-28.jsonl", "2026-11-03.jsonl", DAY + ".jsonl",
                                        "latest.json", "notes.txt"])  # 7 days: 10-28 stays, 10-27 goes

    def test_the_size_cap_deletes_the_oldest_whole_days_first(self):
        for n in ("2026-11-01", "2026-11-02", "2026-11-03"):
            self.day(n, size=600 << 10)  # 3 x 600 KiB over a 1 MiB cap
        self.assertIsNone(self.append(max_mb=1))
        self.assertEqual(self.names(), ["2026-11-03.jsonl", DAY + ".jsonl"])

    def test_when_today_alone_is_over_the_cap_recording_pauses(self):
        p = self.day(DAY, size=(1 << 20) + 10)
        paused = self.append(max_mb=1)
        self.assertIn("cap", paused)
        self.assertEqual(os.path.getsize(p), (1 << 20) + 10)

    def test_low_free_space_pauses_recording(self):
        low = lambda m: types.SimpleNamespace(f_bavail=10, f_frsize=1 << 20, f_blocks=1, f_bfree=1, f_files=1, f_ffree=1)  # noqa: E731
        paused = ml.append_history(self.h, rec(), self.cfg, at(0), statvfs=low)
        self.assertIn("free space", paused)
        self.assertEqual(self.names(), [])

    def test_a_full_disk_pauses_recording_instead_of_raising(self):
        with mock.patch.object(os, "write", side_effect=OSError(errno.ENOSPC, "No space left")):
            paused = self.append()
        self.assertIn("No space", paused)

    def test_a_missing_directory_is_created(self):
        h = str(self.tmp / "new" / "history")
        self.assertIsNone(ml.append_history(h, rec(), self.cfg, at(0), statvfs=roomy))
        self.assertEqual(os.listdir(h), [DAY + ".jsonl"])


class Reading(Base):
    def setUp(self):
        super().setUp()
        self.h = str(self.tmp / "history")
        os.makedirs(self.h)

    def test_corrupt_lines_are_skipped_and_counted(self):
        good = json.dumps(rec(0))
        with open(os.path.join(self.h, DAY + ".jsonl"), "w") as f:
            f.write("\n".join([good, "garbage{", '{"x": 1}', "[1, 2]", '{"t": "not a time"}', json.dumps(rec(5)), ""]))
        got, bad = ml.read_history(self.h, T0 - timedelta(days=1))
        self.assertEqual((len(got), bad), (2, 4))

    def test_an_unreadable_file_counts_as_bad_and_the_rest_is_read(self):
        os.makedirs(os.path.join(self.h, "2026-11-03.jsonl"))  # a directory where a file should be
        open(os.path.join(self.h, DAY + ".jsonl"), "w").write(json.dumps(rec(0)) + "\n")
        got, bad = ml.read_history(self.h, T0 - timedelta(days=2))
        self.assertEqual((len(got), bad), (1, 1))

    def test_only_the_asked_time_range_comes_back_in_order(self):
        with open(os.path.join(self.h, DAY + ".jsonl"), "w") as f:
            f.write("".join(json.dumps(rec(m)) + "\n" for m in (0, 30, 60, 90)))
        got, bad = ml.read_history(self.h, at(25), at(70))
        self.assertEqual(([r["t"] for r in got], bad), ([core.iso(at(30)), core.iso(at(60))], 0))

    def test_no_directory_means_no_records(self):
        self.assertEqual(ml.read_history(str(self.tmp / "none"), T0), ([], 0))


class Pacing(unittest.TestCase):
    cfg = cfgof()

    def due(self, last_min, now_min, unhealthy=False, force=False):
        last = None if last_min is None else core.iso(at(last_min))
        return ml.history_due(last, at(now_min), self.cfg, unhealthy, force)

    def test_healthy_hosts_record_hourly_with_half_a_check_of_slack(self):
        self.assertTrue(self.due(None, 0))
        self.assertFalse(self.due(0, 55))
        self.assertTrue(self.due(0, 57.5))
        self.assertTrue(self.due(0, 60))

    def test_unhealthy_hosts_record_every_five_minutes(self):
        self.assertFalse(self.due(0, 2, unhealthy=True))
        self.assertTrue(self.due(0, 2.5, unhealthy=True))
        self.assertTrue(self.due(0, 5, unhealthy=True))

    def test_transitions_always_record(self):
        self.assertTrue(self.due(0, 1, force=True))

    def test_a_clock_that_went_backwards_records(self):
        self.assertTrue(self.due(100, 0))


class Stats(unittest.TestCase):
    def test_min_avg_max_per_metric_and_per_mount(self):
        rs = [rec(0, cpu=10.0, disk={"/": 70.0}), rec(5, cpu=30.0, disk={"/": 80.0}, swap=None), rec(10, cpu=20.0, disk={"/": 75.0, "/data": 10.0})]
        st = ml.history_stats(rs)
        self.assertEqual(st["cpu"], (10.0, 20.0, 30.0))
        self.assertEqual(st["disk:/"], (70.0, 75.0, 80.0))
        self.assertEqual(st["disk:/data"], (10.0, 10.0, 10.0))
        self.assertEqual(st["swap"], (0.0, 0.0, 0.0))  # a None sample is not a zero sample
        self.assertEqual(ml.history_stats([]), {})


class State(MBase):
    def real_write(self, fail_under):
        real = core.write_json

        def fake(path, obj, mode=0o644):
            if str(path).startswith(fail_under):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, obj, mode)
        return fake

    def test_state_goes_to_the_state_dir(self):
        self.assertTrue(ml.write_state(self.ctx, "latest.json", {"a": 1}, str(self.tmp / "run")))
        self.assertEqual(ml.read_state(self.ctx, "latest.json", None, str(self.tmp / "run")), {"a": 1})
        self.assertFalse((self.tmp / "run").exists())

    def test_a_full_disk_falls_back_to_the_run_dir_and_reads_follow(self):
        run = str(self.tmp / "run")
        with mock.patch.object(core, "write_json", self.real_write(self.ctx.state)):
            self.assertFalse(ml.write_state(self.ctx, "latest.json", {"a": 2}, run))
        self.assertEqual(core.read_json(os.path.join(run, "latest.json")), {"a": 2})
        self.assertEqual(ml.read_state(self.ctx, "latest.json", None, run), {"a": 2})

    def test_the_newer_copy_wins_when_both_exist(self):
        run = str(self.tmp / "run")
        ml.write_state(self.ctx, "x.json", {"v": "old"}, run)
        os.makedirs(run)
        core.write_json(os.path.join(run, "x.json"), {"v": "new"})
        os.utime(self.ctx.path("state", "x.json"), (1, 1))
        self.assertEqual(ml.read_state(self.ctx, "x.json", None, run), {"v": "new"})

    def test_writing_never_raises_even_when_both_places_fail(self):
        with mock.patch.object(core, "write_json", side_effect=OSError(errno.EROFS, "Read-only file system")):
            self.assertIsNone(ml.write_state(self.ctx, "latest.json", {"a": 3}, str(self.tmp / "run")))

    def test_a_corrupt_state_file_reads_as_the_default(self):
        os.makedirs(self.ctx.state)
        open(self.ctx.path("state", "alerts.json"), "w").write("{broken")
        self.assertEqual(ml.read_state(self.ctx, "alerts.json", "dflt", str(self.tmp / "run")), "dflt")
        self.assertEqual(ml.read_state(self.ctx, "missing.json", "dflt", str(self.tmp / "run")), "dflt")


if __name__ == "__main__":
    unittest.main()
