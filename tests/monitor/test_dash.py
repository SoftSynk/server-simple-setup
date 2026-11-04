"""synk-dash: what it gathers (fake /proc, monitor state, history), how it draws at any size, the keys, --once."""
import collections
import curses
import io
import os
import subprocess
import sys
from contextlib import redirect_stdout
from unittest import mock

from montest import MBase, MON, at, cfgof, core, ml, script, statvfs_for, unittest

ATTRS = {"", "bold", "dim", "ok", "warn", "crit", "head"}


class Spark(unittest.TestCase):
    def setUp(self):
        self.s = script("synk-dash").spark

    def test_a_percent_picks_one_of_eight_blocks_and_ascii_has_its_own_set(self):
        self.assertEqual(self.s([0, 100], 2, True), "▁█")
        self.assertEqual(self.s([50], 1, True), "▅")
        self.assertEqual(self.s([0, 100], 2, False), "_#")
        self.assertEqual(self.s([150, -5], 2, True), "█▁")

    def test_a_missing_sample_is_a_gap(self):
        self.assertEqual(self.s([0, None, 100], 3, True), "▁ █")

    def test_long_series_are_averaged_into_buckets_and_short_ones_are_not_stretched(self):
        self.assertEqual(self.s([0] * 5 + [100] * 5, 2, True), "▁█")
        self.assertEqual(self.s([0, 100, 0, 100], 2, True), "▅▅")
        self.assertEqual(len(self.s([1, 2, 3], 10, True)), 3)
        self.assertEqual(self.s([None, None, 80, 80], 2, True), " ▇")  # 80% of 7 steps = 5.6, rounds to step 6 (the 7th block)

    def test_nothing_to_draw(self):
        self.assertEqual((self.s([], 5, True), self.s([1, 2], 0, True)), ("", ""))


class Keys(unittest.TestCase):
    def test_q_quits_r_refreshes_t_flips_the_span_and_a_timeout_refreshes_on_its_own(self):
        step = script("synk-dash").step
        self.assertEqual(step(ord("q"), 24), ("quit", 24))
        self.assertEqual(step(ord("Q"), 24), ("quit", 24))
        self.assertEqual(step(ord("r"), 24), ("refresh", 24))
        self.assertEqual(step(ord("t"), 24), ("refresh", 168))
        self.assertEqual(step(ord("t"), 168), ("refresh", 24))
        self.assertEqual(step(-1, 24), ("refresh", 24))
        self.assertEqual(step(ord("x"), 24), (None, 24))


class Dash(MBase):
    def setUp(self):
        super().setUp()
        self.dash = script("synk-dash")
        self.sleeps = []
        self.at(core.iso(at(0)))

    def sleep(self, s):
        self.sleeps.append(s)

    def collect(self, minutes=0, prev=None, span=24, **env):
        self.at(core.iso(at(minutes)))
        return self.dash.collect(self.ctx, self.env(**env), prev, self.sleep, span)

    def hist(self, minutes, **kw):
        r = {"t": core.iso(at(minutes)), "lvl": "ok", "cpu": 10.0, "mem": 60.0, "swap": 0.0, "load": [0.1] * 3,
             "disk": {"/": 70.0}, "ino": {"/": 5.0}, "rx": 0, "tx": 0, "failed": 0, "down": []}
        r.update(kw)
        paused = ml.append_history(self.ctx.path("data", "history"), r, cfgof()["history"], at(minutes), statvfs=statvfs_for())
        self.assertIsNone(paused)


class Collect(Dash):
    def test_the_first_collect_samples_twice_around_a_short_wait_to_measure_cpu(self):
        self.at(core.iso(at(0)))
        d = self.dash.collect(self.ctx, self.env(), None, lambda s: (self.sleeps.append(s), self.cpu(40)), 24)
        self.assertEqual((self.sleeps, d["cpu"]), ([0.3], 40.0))

    def test_later_collects_reuse_the_previous_raw_sample_and_do_not_wait(self):
        d = self.collect()
        self.cpu(20)
        d2 = self.collect(1, prev=d["raw"])
        self.assertEqual((self.sleeps, d2["cpu"]), ([0.3], 20.0))

    def test_it_reads_the_machine(self):
        d = self.collect()
        self.assertEqual((d["host"], d["mem"], d["swap"], d["span"]), ("web1", 75.0, 25.0, 24))
        self.assertEqual(sorted(d["fs"]), ["/", "/boot/efi", "/mnt/data disk"])
        self.assertEqual(d["fs"]["/"]["percent"], 50.0)
        self.assertIsInstance(d["utf8"], bool)
        self.assertGreater(d["uptime"], 0)

    def test_no_proc_means_unknown_not_zero(self):
        d = self.collect(proc=str(self.tmp / "nope"))
        self.assertIsNone(d["mem"])
        self.assertIsNone(d["cpu"])

    def test_without_a_monitor_it_says_so(self):
        self.assertEqual(self.collect()["mon"]["state"], "missing")

    def test_monitor_data_is_ok_then_stale_after_three_missed_runs(self):
        self.config()
        self.check(0)
        d = self.collect(2)
        self.assertEqual((d["mon"]["state"], d["mon"]["age"], d["mon"]["level"]), ("ok", 120, 0))
        self.assertEqual(d["svc"]["nginx.service"], "active")
        self.assertEqual(self.collect(14)["mon"]["state"], "ok")
        self.assertEqual(self.collect(16)["mon"]["state"], "stale")

    def test_the_open_incident_and_the_last_recoveries_come_from_the_alert_state(self):
        self.config()
        self.disk["/"] = 93.0
        self.check(0)
        d = self.collect(1)
        self.assertEqual((d["incident"]["peak"], d["mon"]["level"], d["closed"]), (2, 2, []))
        self.assertEqual(d["checks"]["disk:/"]["value"], 93.0)
        self.disk["/"] = 50.0
        self.check(5), self.check(10)
        d = self.collect(11)
        self.assertIsNone(d["incident"])
        self.assertEqual(d["closed"][0]["keys"], ["disk:/"])

    def test_a_broken_config_does_not_stop_the_dashboard(self):
        core.write_json(self.ctx.config_path, {"thresholds": {"cpu": {"crit": 101}}})
        self.assertEqual(self.collect()["mem"], 75.0)

    def test_the_trend_covers_the_chosen_span_only(self):
        self.hist(-3 * 24 * 60, mem=10.0)
        self.hist(-60, mem=50.0)
        self.hist(-30, mem=70.0)
        self.assertEqual(self.collect()["trend"]["memory"], [50.0, 70.0])
        self.assertEqual(self.collect(span=168)["trend"]["memory"], [10.0, 50.0, 70.0])
        self.assertEqual(self.collect()["trend"]["disk:/"], [70.0, 70.0])

    def test_no_history_is_an_empty_trend(self):
        self.assertEqual(self.collect()["trend"], {})


class Render(Dash):
    def data(self, minutes=1, **over):
        d = self.collect(minutes)
        d.update(over)
        return d

    def lines(self, width=100, height=None, **over):
        return self.dash.render(self.data(**over), width, height)

    def find(self, lines, *needles):
        return [(t, a) for t, a in lines if all(n in t for n in needles)]

    def test_every_line_fits_every_screen_and_uses_a_known_attribute(self):
        self.config()
        self.disk["/"] = 93.0
        self.check(0)
        self.hist(-60)
        d = self.collect(1)
        for w in (12, 20, 40, 80, 200):
            for h in (None, 1, 2, 3, 6, 24):
                out = self.dash.render(d, w, h)
                self.assertTrue(out)
                for text, attr in out:
                    self.assertLessEqual(len(text), w, (w, h, text))
                    self.assertIn(attr, ATTRS)
                if h:
                    self.assertLessEqual(len(out), h)

    def test_a_short_screen_keeps_the_header_and_the_footer(self):
        d = self.data()
        out = self.dash.render(d, 100, 3)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0][1], "head")
        self.assertIn("softsynk", out[0][0])
        self.assertIn("web1", out[0][0])
        self.assertIn("q quit", out[-1][0])
        two = self.dash.render(d, 100, 2)
        self.assertEqual((len(two), "softsynk" in two[0][0], "q quit" in two[1][0]), (2, True, True))
        self.assertEqual(len(self.dash.render(d, 100, 1)), 1)

    def test_a_healthy_host_reads_like_a_status_page(self):
        self.config()
        self.check(0)
        out = self.lines(100)
        mem = self.find(out, "memory", "75%")
        self.assertEqual([a for _, a in mem], [""])
        self.assertTrue(self.find(out, "swap", "25%"))
        self.assertTrue(self.find(out, "disk /", "50%"))
        self.assertTrue(self.find(out, "/mnt/data disk"))
        self.assertEqual([a for _, a in self.find(out, "monitor", "OK", "1m ago")], ["ok"])
        self.assertTrue(self.find(out, "up "))
        self.assertTrue(self.find(out, "load"))
        self.assertTrue(self.find(out, "nginx.service"))
        self.assertTrue(self.find(out, "failed units", "none"))
        self.assertTrue(self.find(out, "no open incident"))
        self.assertIn("/usr/local/bin/synk-monitor report", out[-1][0])
        self.assertIn("q quit", out[-1][0])

    def test_rows_turn_warn_and_crit_by_the_configured_thresholds(self):
        self.disk["/"] = 93.0
        self.disk["/mnt/data disk"] = 85.0
        out = self.lines(100, mem=97.0, swap=82.0, cpu=40.0)
        self.assertEqual([a for _, a in self.find(out, "disk /", "93%")], ["crit"])
        self.assertEqual([a for _, a in self.find(out, "/mnt/data disk", "85%")], ["warn"])
        self.assertEqual([a for _, a in self.find(out, "memory", "97%")], ["crit"])
        self.assertEqual([a for _, a in self.find(out, "swap", "82%")], ["warn"])
        self.assertEqual([a for _, a in self.find(out, "cpu", "40%")], [""])

    def test_inode_trouble_shows_on_its_disk_row(self):
        self.inodes["/"] = 95.0
        out = self.lines(100)
        self.assertEqual([a for _, a in self.find(out, "disk /", "ino 95%")], ["crit"])

    def test_the_monitor_line_follows_the_monitor(self):
        out = self.lines(100)
        row = self.find(out, "monitor not installed")
        self.assertEqual([a for _, a in row], ["warn"])
        self.assertIn("/usr/local/bin/synk-monitor setup", row[0][0])
        self.config()
        self.check(0)
        self.assertEqual([a for _, a in self.find(self.lines(100, minutes=16), "monitor", "STALE")], ["warn"])
        self.disk["/"] = 93.0
        self.check(20)
        self.assertEqual([a for _, a in self.find(self.lines(100, minutes=21), "monitor", "CRITICAL")], ["crit"])

    def test_services_and_failed_units(self):
        self.config()
        self.runner.answers["systemctl is-active"] = "inactive\nactive\nactive\n"
        self.runner.answers["systemctl list-units --failed"] = "● a.service loaded failed failed A\n"
        self.check(0)
        out = self.lines(100)
        self.assertEqual([a for _, a in self.find(out, "nginx.service", "down")], ["crit"])
        self.assertEqual([a for _, a in self.find(out, "failed units", "a.service")], ["warn"])

    def test_the_alerts_panel_shows_the_open_incident_and_recent_recoveries(self):
        self.config()
        self.disk["/"] = 93.0
        self.check(0)
        out = self.lines(100)
        row = self.find(out, "incident", "disk / 93%")
        self.assertEqual([a for _, a in row], ["crit"])
        self.disk["/"] = 50.0
        self.check(5), self.check(10)
        out = self.lines(100, minutes=11)
        self.assertEqual([a for _, a in self.find(out, "recovered", "disk /", "10m")], ["dim"])
        self.assertTrue(self.find(out, "no open incident"))

    def test_the_network_row_shows_rates(self):
        out = self.lines(100, rx=1536, tx=0)
        self.assertTrue(self.find(out, "net", "1.5 KiB/s", "0 B/s"))

    def test_sparklines_follow_the_span_and_the_header_names_it(self):
        self.hist(-3 * 24 * 60, mem=10.0)
        self.hist(-60, mem=50.0)
        self.hist(-30, mem=70.0)
        out = self.lines(100)
        self.assertTrue(self.find(out, "memory", "▅▆"))
        self.assertFalse(self.find(out, "memory", "▂▅▆"))
        self.assertIn("24h", out[0][0])
        d = self.collect(1, span=168)
        out = self.dash.render(d, 100, None)
        self.assertTrue(self.find(out, "memory", "▂▅▆"))
        self.assertIn("7d", out[0][0])

    def test_a_narrow_screen_drops_the_sparklines(self):
        self.hist(-60, mem=50.0)
        self.hist(-30, mem=70.0)
        self.assertFalse(self.find(self.lines(40), "▅▆"))

    def test_without_utf8_everything_is_ascii(self):
        self.config()
        self.disk["/"] = 93.0
        self.check(0)
        self.hist(-60)
        self.hist(-30)
        self.assertTrue(all(t.isascii() for t, _ in self.lines(100, utf8=False)))
        self.assertFalse(all(t.isascii() for t, _ in self.lines(100, utf8=True)))

    def test_without_proc_the_rows_say_n_a(self):
        d = self.collect(proc=str(self.tmp / "nope"))
        out = self.dash.render(d, 100, None)
        self.assertTrue(self.find(out, "cpu", "n/a"))
        self.assertTrue(self.find(out, "memory", "n/a"))
        self.assertTrue(self.find(out, "swap", "n/a"))

    def test_an_unreadable_mount_says_n_a(self):
        def broken(mount):
            raise OSError("gone")
        d = self.collect(statvfs=broken)
        self.assertTrue(self.find(self.dash.render(d, 100, None), "disk /", "n/a"))


class Screen:
    """Just enough of a curses window for the loop."""
    def __init__(self, keys, size=(24, 80)):
        self.keys, self.size, self.frames, self.wait = list(keys), size, [], None

    def timeout(self, ms):
        self.wait = ms

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.frames.append([])

    def addnstr(self, y, x, text, n, attr=0):
        self.frames[-1].append((y, x, text[:n], attr))

    def refresh(self):
        pass

    def getch(self):
        return self.keys.pop(0)


class Loop(Dash):
    def run_loop(self, scr):
        self.dash.loop(scr, self.ctx, self.env(), self.sleep, collections.defaultdict(int))

    def test_r_and_t_redraw_and_q_stops_and_the_machine_is_sampled_only_once_cold(self):
        scr = Screen([ord("r"), ord("t"), ord("q")])
        self.run_loop(scr)
        self.assertEqual((len(scr.frames), scr.wait, self.sleeps), (3, 2000, [0.3]))
        self.assertIn("24h", scr.frames[0][0][2])
        self.assertIn("7d", scr.frames[2][0][2])
        self.assertTrue(all(x == 0 and y < 24 and len(t) < 80 for f in scr.frames for y, x, t, _ in f))

    def test_a_tiny_or_failing_screen_does_not_crash_it(self):
        self.run_loop(Screen([ord("q")], size=(2, 10)))

        class Broken(Screen):
            def addnstr(self, *a):
                raise curses.error("no room")
        self.run_loop(Broken([-1, ord("q")]))


class Once(Dash):
    def setUp(self):
        super().setUp()
        self.dash.ENV = self.env()
        self.dash.SLEEP = lambda s: None

    def run_dash(self, *argv, tty=False, columns="100"):
        class Out(io.StringIO):
            def isatty(self):
                return tty
        out = Out()
        with redirect_stdout(out), mock.patch.dict(os.environ, COLUMNS=columns, LINES="30"):
            rc = self.dash.main(["--root", str(self.root), *argv])
        return rc, out.getvalue()

    def test_once_prints_the_whole_page_as_plain_text(self):
        rc, out = self.run_dash("--once")
        self.assertEqual(rc, 0)
        self.assertIn("softsynk", out.splitlines()[0])
        self.assertIn("web1", out)
        self.assertIn("synk-monitor setup", out)  # no monitor in this root
        self.assertNotIn("\x1b", out)
        self.assertGreater(len(out.splitlines()), 10)

    def test_it_uses_the_terminal_width(self):
        rc, out = self.run_dash("--once", columns="50")
        self.assertTrue(all(len(line) <= 50 for line in out.splitlines()))

    def test_when_stdout_is_not_a_terminal_it_prints_once_without_curses(self):
        with mock.patch.object(self.dash, "run_curses", side_effect=AssertionError("curses used")):
            rc, out = self.run_dash()
        self.assertEqual(rc, 0)
        self.assertIn("softsynk", out)

    def test_on_a_terminal_the_curses_screen_runs(self):
        with mock.patch.object(self.dash, "run_curses") as cur:
            rc, out = self.run_dash(tty=True)
        self.assertEqual((rc, out, cur.call_count), (0, "", 1))

    def test_version(self):
        rc, out = self.run_dash("--version")
        self.assertEqual((rc, out), (0, "synk-dash 1.0.0\n"))

    def test_the_real_script_prints_a_page_even_where_there_is_no_proc(self):
        root = os.path.join(os.environ.get("TMPDIR", "/tmp"), "softsynk-dash-smoke")
        env = {k: v for k, v in os.environ.items() if not k.startswith("SOFTSYNK_")}
        r = subprocess.run([sys.executable, str(MON / "synk-dash"), "--root", root, "--once"], capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("softsynk", r.stdout)
        self.assertFalse(os.path.exists(root))  # looking creates nothing


if __name__ == "__main__":
    unittest.main()
