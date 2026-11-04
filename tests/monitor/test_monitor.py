"""check_once end to end (fake /proc, stub mail), the synk-monitor commands, the unit templates and the manifest."""
import errno
import io
import json
import os
import subprocess
import sys
import tomllib
import types
from contextlib import redirect_stderr, redirect_stdout

from montest import MBase, MON, MOUNTS, REPO, at, core, ml, mock, script, unittest

UNITS = ("softsynk-monitor.service", "softsynk-monitor.timer")


def log_events(ctx):
    d = ctx.log
    out = []
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else ():
        out += [json.loads(line) for line in open(os.path.join(d, n)) if line.strip()]
    return out


class CheckOnce(MBase):
    def setUp(self):
        super().setUp()
        self.config()

    def history(self):
        return ml.read_history(self.ctx.path("data", "history"), at(-24 * 60 * 30), at(24 * 60 * 30))

    def test_a_healthy_host_writes_state_and_history_and_sends_nothing(self):
        self.check(0)
        got = self.latest()
        self.assertEqual((got["level"], got["snap"]["mem"], got["snap"]["swap"]), (0, 75.0, 25.0))
        self.assertEqual(sorted(got["snap"]["fs"]), sorted(MOUNTS))
        self.assertEqual(got["snap"]["svc"], {"nginx.service": "active", "php8.3-fpm.service": "active", "redis-server.service": "active"})
        self.assertIsNone(got["snap"]["cpu"])  # no previous sample: unmeasured, not zero
        self.assertEqual(self.mail.sent, [])
        self.assertEqual(self.alerts()["pending"], [])
        recs, bad = self.history()
        self.assertEqual((len(recs), bad), (1, 0))
        self.assertEqual((recs[0]["lvl"], recs[0]["mem"], sorted(recs[0]["disk"])), ("ok", 75.0, sorted(MOUNTS)))

    def test_cpu_and_network_are_rates_between_two_runs(self):
        self.check(0)
        self.cpu(40)
        self.check(5)
        snap = self.latest()["snap"]
        self.assertEqual((snap["cpu"], snap["rx"], snap["tx"]), (40.0, 0, 0))  # the net counters did not move

    def test_a_cpu_counter_that_went_backwards_is_unmeasured(self):
        self.check(0)
        self.busy, self.idle = 10, 10
        self.cpu(0, total=0)
        self.check(5)
        self.assertIsNone(self.latest()["snap"]["cpu"])

    def test_a_full_disk_mails_what_to_look_at_and_does_not_repeat_itself(self):
        self.disk["/"] = 93.0
        self.check(0)
        self.assertEqual(self.mail.subjects, ["CRITICAL: disk / 93%"])
        body = self.mail.sent[0][1]
        for text in ("web1", "disk /", "93%", "warn 80%", "crit 90%", "since", "var", "40.0 GiB", "synk-clean scan",
                     "/usr/local/bin/synk-dash", "/usr/local/bin/synk-monitor report"):
            self.assertIn(text, body)
        self.assertEqual(self.dirs.call_args.args[0], "/")
        self.assertEqual(self.alerts()["pending"], [])
        self.assertEqual([e["event"] for e in log_events(self.ctx) if e["event"].startswith("monitor.")], ["monitor.open"])
        self.check(5), self.check(10)
        self.assertEqual(len(self.mail.sent), 1)

    def test_recovery_is_mailed_with_the_incident_length(self):
        self.disk["/"] = 93.0
        self.check(0)
        self.disk["/"] = 50.0
        self.check(5)
        self.assertEqual(len(self.mail.sent), 1)  # one calm sample is not recovery yet
        self.check(10)
        self.assertEqual(self.mail.subjects[1], "OK: recovered after 10m (disk /)")
        self.assertIn("peak CRITICAL", self.mail.sent[1][1])
        self.assertIsNone(self.alerts()["incident"])
        self.assertEqual(self.alerts()["closed"][0]["keys"], ["disk:/"])

    def test_a_down_service_says_how_to_find_out_why(self):
        self.runner.answers["systemctl is-active"] = "inactive\nactive\nactive\n"
        self.check(0)
        self.assertEqual(self.mail.subjects, ["CRITICAL: nginx.service down"])
        self.assertIn("/usr/bin/journalctl -u nginx.service -n 100 --no-pager", self.mail.sent[0][1])
        self.assertIn("/usr/bin/systemctl restart nginx.service", self.mail.sent[0][1])

    def test_failed_units_are_listed_and_ignored_ones_are_not_counted(self):
        self.config(ignore_units=["old.service"])
        self.runner.answers["systemctl list-units --failed"] = "● old.service loaded failed failed Old\n● a.service loaded failed failed A\n● b.service loaded failed failed B\n"
        self.check(0), self.check(5)
        self.assertEqual(self.latest()["snap"]["failed"], ["a.service", "b.service"])
        self.assertEqual(self.mail.subjects, ["WARNING: 2 failed units"])
        self.assertIn("a.service", self.mail.sent[0][1])

    def test_extra_trouble_is_counted_in_the_subject(self):
        self.disk["/"] = 93.0
        self.inodes["/"] = 85.0
        self.check(0)
        self.assertEqual(self.mail.subjects, ["CRITICAL: disk / 93% (+1 more)"])

    def test_a_cpu_alert_names_the_busiest_processes(self):
        for i in range(4):
            self.cpu(97)
            self.check(5 * i)
        self.assertEqual(self.mail.subjects, ["CRITICAL: cpu 97%"])
        self.assertIn("php-fpm8.3", self.mail.sent[0][1])
        self.assertEqual(self.top.call_args.kwargs.get("by"), "cpu")

    def test_a_failed_mail_is_kept_and_retried_after_fifteen_minutes(self):
        self.mail.ok = False
        self.disk["/"] = 93.0
        self.check(0)
        pending = self.alerts()["pending"]
        self.assertEqual((len(pending), pending[0]["tries"]), (1, 1))
        self.assertIn("SMTPServerDisconnected", pending[0]["error"])
        self.mail.ok = True
        self.check(5)
        self.assertEqual(len(self.mail.sent), 1)  # not yet
        self.check(15)
        self.assertEqual(len(self.mail.sent), 2)
        self.assertEqual(self.alerts()["pending"], [])
        self.check(20)
        self.assertEqual(len(self.mail.sent), 2)

    def test_a_mount_that_cannot_be_read_does_not_close_the_incident(self):
        self.disk["/"] = 93.0
        self.check(0)

        def broken(mount):
            raise OSError(errno.EIO, "I/O error")
        self.check(5, statvfs=broken)
        self.check(10, statvfs=broken)
        self.assertEqual(len(self.mail.sent), 1)
        self.assertEqual(self.latest()["level"], 2)

    def test_history_is_hourly_when_healthy_and_every_five_minutes_in_trouble(self):
        for m in (0, 5, 10):
            self.check(m)
        self.assertEqual(len(self.history()[0]), 1)
        self.disk["/"] = 85.0
        self.check(15)
        self.check(20)
        self.assertEqual([r["lvl"] for r in self.history()[0]], ["ok", "warn", "warn"])
        self.disk["/"] = 50.0
        self.check(25), self.check(30)  # the recovery is recorded at once
        self.assertEqual([r["lvl"] for r in self.history()[0]][-1], "ok")
        self.assertEqual(len(self.history()[0]), 5)

    def test_a_full_state_disk_falls_back_to_run_and_never_crashes(self):
        real = core.write_json

        def fake(path, obj, mode=0o644):
            if str(path).startswith(self.ctx.state):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, obj, mode)
        with mock.patch.object(core, "write_json", fake):
            self.disk["/"] = 93.0
            self.check(0)
        run = str(self.tmp / "run")
        self.assertFalse(core.read_json(os.path.join(run, "latest.json"))["stored"])
        self.assertEqual(core.read_json(os.path.join(run, "alerts.json"))["pending"], [])
        self.assertEqual(len(self.mail.sent), 1)
        self.check(5)  # the next run finds the incident in /run and stays quiet
        self.assertEqual(len(self.mail.sent), 1)

    def test_a_paused_history_is_reported_not_fatal(self):
        full = types.SimpleNamespace(f_bavail=1, f_frsize=1 << 20, f_blocks=1000, f_bfree=1, f_files=1000, f_ffree=900)
        self.check(0, statvfs=lambda m: full)
        self.assertIn("free space", self.latest()["history"])

    def test_corrupt_alert_state_starts_fresh(self):
        os.makedirs(self.ctx.state)
        open(self.ctx.path("state", "alerts.json"), "w").write("{broken")
        self.check(0)
        self.assertEqual(self.alerts()["pending"], [])

    def test_an_invalid_config_is_a_usage_error_and_no_config_means_defaults(self):
        core.write_json(self.ctx.config_path, {"thresholds": {"cpu": {"crit": 101}}})
        with self.assertRaises(core.Fail) as cm:
            self.check(0)
        self.assertEqual(cm.exception.code, core.USAGE)
        os.unlink(self.ctx.config_path)
        self.check(5)
        self.assertEqual(self.latest()["snap"]["svc"], {})

    def test_the_weekly_summary_is_mailed_once_a_week(self):
        self.check(0)
        self.check(7 * 24 * 60)
        self.assertEqual(self.mail.subjects, ["Weekly summary: no incidents"])
        self.assertIn("memory", self.mail.sent[0][1])
        self.check(7 * 24 * 60 + 5)
        self.assertEqual(len(self.mail.sent), 1)


class Mails(unittest.TestCase):
    """render_mail is text from data: no I/O."""
    def ev(self, kind="open", level=2, **checks):
        checks = checks or {"disk:/": {"level": 2, "value": 93.0, "since": "2026-11-04T10:00:00+00:00"}}
        return {"id": "x", "kind": kind, "t": "2026-11-04T12:10:00+00:00", "level": level, "checks": checks,
                "incident": {"opened": "2026-11-04T10:00:00+00:00", "peak": level, "keys": sorted(checks)}}

    def render(self, ev, **kw):
        extras = dict({"dirs": {}, "procs": {}, "incidents": 0}, **kw)
        return ml.render_mail(ev, ml.DEFAULTS, "web1", None, {"cpu": (1.0, 5.5, 9.0)}, extras, at(130))

    def test_subjects_by_kind(self):
        self.assertEqual(self.render(self.ev("escalate"))[0], "CRITICAL: disk / 93% (escalated)")
        self.assertEqual(self.render(self.ev("reminder"))[0], "CRITICAL: disk / 93% (reminder)")
        self.assertEqual(self.render(self.ev("open", 1, **{"memory": {"level": 1, "value": 88.0, "since": None}}))[0], "WARNING: memory 88%")
        self.assertEqual(self.render(self.ev("reminder", 2, **{"disk:/": {"level": 2, "value": 93.0, "since": None},
                                                                "swap": {"level": 1, "value": 85.0, "since": None}}))[0],
                         "CRITICAL: disk / 93% (+1 more, reminder)")

    def test_the_overview_and_the_24_hour_numbers_are_in_the_body(self):
        body = self.render(self.ev())[1]
        self.assertIn("incident open for 2h 10m", body)
        self.assertIn("cpu", body)
        self.assertIn("1.0 / 5.5 / 9.0", body)

    def test_recommended_actions_follow_the_kind_of_trouble(self):
        body = self.render(self.ev(**{"inodes:/data": {"level": 1, "value": 85.0, "since": None}}))[1]
        self.assertIn("/usr/bin/du --inodes -x --max-depth=2 /data", body)
        body = self.render(self.ev(**{"memory": {"level": 2, "value": 97.0, "since": None}}), procs={"rss": [{"name": "php-fpm8.3", "rss": 900 << 20, "cpu": 5.0, "pid": 7}]})[1]
        self.assertIn("php-fpm8.3", body)
        self.assertIn("900.0 MiB", body)

    def test_the_summary_counts_incidents_and_a_report_shows_the_level(self):
        ev = {"id": "s", "kind": "summary", "t": "2026-11-11T10:00:00+00:00", "level": 0, "checks": {}, "incident": None}
        self.assertEqual(self.render(ev, incidents=2)[0], "Weekly summary: 2 incidents")
        ev = dict(self.ev(), kind="report")
        self.assertEqual(self.render(ev)[0], "Status report: CRITICAL")


class Cli(MBase):
    def setUp(self):
        super().setUp()
        self.mon = script("synk-monitor")
        self.mon.ENV = self.env()
        self.at(core.iso(at(0)))
        self.runner.answers["systemctl is-active"] = "active\n"
        self.udir = self.tmp / "units"

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.mon.main(["--root", str(self.root), *argv])
        return rc, out.getvalue(), err.getvalue()

    def setup(self, *extra):
        return self.run_cli("setup", "--services", "nginx.service", *extra)

    def status(self):
        rc, out, _ = self.run_cli("--status-json")
        self.assertEqual(rc, 0)
        return json.loads(out)

    def test_version(self):
        self.assertEqual(self.run_cli("--version")[1], "synk-monitor 1.0.0\n")

    def test_setup_writes_config_units_enables_the_timer_and_takes_a_first_sample(self):
        rc, _, err = self.setup("--interval", "120")
        self.assertEqual(rc, 0, err)
        cfg = core.read_json(self.ctx.config_path)
        self.assertEqual((cfg["services"], cfg["check_interval_sec"]), (["nginx.service"], 120))
        service, timer = [(self.udir / n).read_text() for n in UNITS]
        self.assertIn(f"ExecStart={os.path.realpath(self.mon.__file__)} --root {self.root} check", service)
        self.assertIn("OnUnitActiveSec=120s", timer)
        self.assertNotIn("@", service + timer)
        self.assertTrue(self.runner.ran("systemctl daemon-reload"))
        self.assertTrue(self.runner.ran("systemctl enable --now softsynk-monitor.timer"))
        self.assertTrue(os.path.exists(self.ctx.path("state", "latest.json")))
        self.assertIn("synk-notify configure", err)  # mail is not set up in this root

    def test_failure_mails_are_wired_only_when_the_alert_unit_exists(self):
        self.setup()
        self.assertNotIn("OnFailure", (self.udir / UNITS[0]).read_text())
        (self.udir / "softsynk-alert@.service").write_text("[Service]\n")
        self.setup()
        self.assertIn("OnFailure=softsynk-alert@%n.service", (self.udir / UNITS[0]).read_text())

    def test_setup_twice_changes_nothing_the_second_time(self):
        self.setup()
        reloads = sum(1 for c in self.runner.calls if c[:2] == ["systemctl", "daemon-reload"])
        self.setup()
        self.assertEqual(sum(1 for c in self.runner.calls if c[:2] == ["systemctl", "daemon-reload"]), reloads)

    def test_dry_run_writes_nothing(self):
        rc, _, _ = self.setup("--dry-run")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.ctx.config_path))
        self.assertFalse(self.udir.exists())
        self.assertFalse(self.runner.ran("systemctl enable"))

    def test_setup_proposes_the_running_services_it_knows(self):
        self.runner.answers["systemctl list-units --type=service --state=running"] = (
            "cron.service  loaded active running Cron\nnginx.service loaded active running Web\nssh.service loaded active running SSH\n"
            "php8.3-fpm.service loaded active running PHP\npostgresql@16-main.service loaded active running PG\n")
        rc, _, err = self.run_cli("setup", "--accept-defaults")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(core.read_json(self.ctx.config_path)["services"]),
                         ["cron.service", "nginx.service", "php8.3-fpm.service", "postgresql@16-main.service"])

    def test_setup_refuses_bad_input(self):
        self.assertEqual(self.run_cli("setup", "--services", "nginx.service; rm -rf /")[0], core.USAGE)
        self.assertEqual(self.run_cli("setup", "--services", "nginx.service", "--interval", "5")[0], core.USAGE)
        self.assertFalse(os.path.exists(self.ctx.config_path))

    def test_check_runs_once_and_skips_quietly_when_another_run_holds_the_lock(self):
        self.setup()
        os.unlink(self.ctx.path("state", "latest.json"))
        with core.lock(self.ctx):
            rc, _, err = self.run_cli("check")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.ctx.path("state", "latest.json")))
        self.assertEqual(self.run_cli("check")[0], 0)
        self.assertTrue(os.path.exists(self.ctx.path("state", "latest.json")))

    def test_status_of_a_root_that_was_never_set_up(self):
        doc = self.status()
        self.assertEqual((doc["module"], doc["install_state"], doc["health"]), ("monitor", "not_installed", "unknown"))
        self.assertEqual(self.run_cli("--status")[0], 3)

    def test_status_follows_the_host_and_flags_problems_of_the_monitor_itself(self):
        self.setup()
        doc = self.status()
        self.assertEqual((doc["install_state"], doc["health"]), ("installed", "degraded"))  # notify is not configured
        self.assertTrue(any("synk-notify configure" in w for w in doc["warnings"]))
        core.write_json(self.ctx.other("notify").config_path, {"transport": "sendmail", "to": ["a@b.co"], "from": "x@y.co"})
        self.assertEqual(self.status()["health"], "healthy")
        self.assertEqual(self.run_cli("--status")[0], 0)
        self.mon.ENV = self.env()
        self.disk["/"] = 93.0
        self.check(5)
        doc = self.status()
        self.assertEqual(doc["health"], "unhealthy")
        self.assertIn("disk / 93%", doc["summary"])
        self.assertEqual(self.run_cli("--status")[0], 2)

    def test_status_notices_stale_data_failing_mail_and_a_timer_that_disagrees(self):
        core.write_json(self.ctx.other("notify").config_path, {"transport": "sendmail", "to": ["a@b.co"], "from": "x@y.co"})
        self.setup()
        self.at(core.iso(at(16)))  # interval 300 s: data older than 3 intervals
        self.assertEqual(self.status()["health"], "stale")
        self.at(core.iso(at(0)))
        (self.udir / UNITS[1]).write_text((self.udir / UNITS[1]).read_text().replace("300s", "60s"))
        self.assertTrue(any("timer" in w for w in self.status()["warnings"]))
        self.mail.ok = False
        self.disk["/"] = 93.0
        self.check(0)
        self.assertTrue(any("mail" in w and "failing" in w for w in self.status()["warnings"]))

    def test_status_says_when_history_is_not_being_recorded(self):
        self.setup()
        path = self.ctx.path("state", "latest.json")
        doc = core.read_json(path)
        core.write_json(path, dict(doc, history="free space is below 64 MB"))
        self.assertTrue(any("history" in w and "64 MB" in w for w in self.status()["warnings"]))

    def test_status_json_always_answers(self):
        self.setup()
        core.write_json(self.ctx.config_path, {"thresholds": {"cpu": {"crit": 101}}})
        doc = self.status()
        self.assertEqual(doc["health"], "unhealthy")
        with mock.patch.object(ml, "read_state", side_effect=RuntimeError("boom")):
            self.assertEqual(self.status()["health"], "unknown")

    def test_report_shows_now_and_can_be_mailed_or_dumped_as_json(self):
        self.setup()
        self.disk["/"] = 85.0
        self.check(5)
        rc, out, _ = self.run_cli("report")
        self.assertEqual(rc, 0)
        for text in ("web1", "WARNING", "disk /", "85%", "Last 24 h"):
            self.assertIn(text, out)
        doc = json.loads(self.run_cli("report", "--json")[1])
        self.assertEqual((doc["level"], doc["host"]), (1, "web1"))
        self.assertIn("disk:/", doc["checks"])
        before = len(self.mail.sent)
        self.assertEqual(self.run_cli("report", "--send")[0], 0)
        self.assertEqual(self.mail.subjects[before:], ["Status report: WARNING"])
        self.mail.ok = False
        self.assertEqual(self.run_cli("report", "--send")[0], core.FAILED)

    def test_report_without_data_says_so(self):
        rc, out, err = self.run_cli("report")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("synk-monitor check", err)

    def test_test_alert_sends_a_marked_sample_and_leaves_state_alone(self):
        rc, _, _ = self.run_cli("test-alert")
        self.assertEqual(rc, 0)
        self.assertEqual(self.mail.subjects, ["TEST: CRITICAL: disk / 93%"])
        self.assertIn("test", self.mail.sent[0][1].lower())
        self.assertFalse(os.path.exists(self.ctx.path("state", "alerts.json")))
        self.mail.ok = False
        self.assertEqual(self.run_cli("test-alert")[0], core.FAILED)


class Packaging(unittest.TestCase):
    def test_templates_have_only_known_placeholders(self):
        for name in UNITS:
            text = (MON / "systemd" / (name + ".in")).read_text()
            for ph in ("@ROOT@", "@BIN@", "@ONFAILURE@", "@INTERVAL@"):
                text = text.replace(ph, "x")
            self.assertNotIn("@", text, name)

    def test_the_manifest_matches_the_contract(self):
        m = tomllib.loads((MON / "module.toml").read_text())
        self.assertEqual((m["schema"], m["id"], m["interface"], m["after"]), (1, "monitor", 1, ["notify"]))
        self.assertEqual(m["platform"], {"os": ["ubuntu"], "min_version": "24.04"})
        self.assertEqual(m["operations"]["status"], ["synk-monitor", "--status-json"])
        self.assertEqual(m["operations"]["install"], ["synk-monitor", "setup"])
        self.assertEqual(sorted(m["commands"]), ["synk-dash", "synk-monitor"])
        for cmd in m["commands"].values():
            self.assertTrue(os.access(MON / cmd, os.X_OK), cmd)

    def test_the_real_script_runs_and_answers_status_in_a_fresh_root(self):
        root = os.path.join(os.environ.get("TMPDIR", "/tmp"), "softsynk-mon-smoke")
        env = {k: v for k, v in os.environ.items() if not k.startswith("SOFTSYNK_")}
        r = subprocess.run([sys.executable, str(MON / "synk-monitor"), "--root", root, "--status-json"], capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["install_state"], "not_installed")
        self.assertFalse(os.path.exists(root))  # reading status creates nothing


if __name__ == "__main__":
    unittest.main()
