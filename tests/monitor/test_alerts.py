"""The alert state machine (monitorlib.evaluate and friends): pure functions, injected clock, no I/O."""
import copy
import json

from montest import T0, at, cfgof, ml, timedelta, unittest

DISK = "disk:/"
SVC = "service:nginx.service"


def kinds(events):
    return [e["kind"] for e in events]


class Machine(unittest.TestCase):
    def setUp(self):
        self.cfg = cfgof()
        self.s = ml.new_state()

    def go(self, minutes, values):
        self.s, events = ml.evaluate(self.s, values, self.cfg, at(minutes))
        return events

    def deliver(self, events, minutes, ok=True):
        for e in events:
            self.s = ml.delivered(self.s, e["id"], ok, at(minutes), "" if ok else "refused")


class Levels(Machine):
    def test_a_warning_needs_sustain_consecutive_samples(self):
        self.assertEqual(self.go(0, {"cpu": 90.0}), [])  # cpu sustain is 3
        self.assertEqual(self.go(5, {"cpu": 90.0}), [])
        ev = self.go(10, {"cpu": 90.0})
        self.assertEqual(kinds(ev), ["open"])
        self.assertEqual((ev[0]["level"], ev[0]["checks"]["cpu"]["level"]), (1, 1))
        self.assertEqual(ml.overall(self.s), 1)

    def test_a_blip_resets_the_count(self):
        for m, v in enumerate([90.0, 90.0, 10.0, 90.0, 90.0]):
            self.assertEqual(self.go(m * 5, {"cpu": v}), [])
        self.assertEqual(kinds(self.go(25, {"cpu": 90.0})), ["open"])

    def test_critical_has_its_own_sustain_and_opens_at_level_two(self):
        self.go(0, {"cpu": 97.0}), self.go(5, {"cpu": 97.0})
        ev = self.go(10, {"cpu": 97.0})
        self.assertEqual((kinds(ev), ev[0]["level"]), (["open"], 2))

    def test_disk_alerts_on_the_first_sample(self):
        ev = self.go(0, {DISK: 85.0})
        self.assertEqual((kinds(ev), ev[0]["level"]), (["open"], 1))

    def test_a_service_that_is_down_is_critical_at_once(self):
        ev = self.go(0, {SVC: 1.0})
        self.assertEqual((kinds(ev), ev[0]["level"], ev[0]["checks"][SVC]["value"]), (["open"], 2, 1.0))

    def test_failed_units_are_a_warning_after_two_samples(self):
        self.assertEqual(self.go(0, {"failed_units": 2.0}), [])
        ev = self.go(5, {"failed_units": 2.0})
        self.assertEqual((kinds(ev), ev[0]["level"]), (["open"], 1))

    def test_warning_then_critical_escalates_once(self):
        self.deliver(self.go(0, {DISK: 85.0}), 0)
        ev = self.go(5, {DISK: 92.0})
        self.assertEqual((kinds(ev), ev[0]["level"]), (["escalate"], 2))
        self.deliver(ev, 5)
        self.assertEqual(self.go(10, {DISK: 93.0}), [])

    def test_a_new_critical_check_escalates_inside_a_critical_incident(self):
        self.go(0, {DISK: 92.0})
        ev = self.go(5, {DISK: 92.0, SVC: 1.0})
        self.assertEqual(kinds(ev), ["escalate"])
        self.assertEqual(sorted(ev[0]["checks"]), [DISK, SVC])

    def test_a_new_warning_inside_an_open_incident_is_silent_until_the_reminder(self):
        self.deliver(self.go(0, {DISK: 85.0}), 0)
        self.assertEqual(self.go(5, {DISK: 85.0, "memory": 90.0}), [])
        self.assertEqual(self.go(10, {DISK: 85.0, "memory": 90.0}), [])  # memory sustain 2: now raised, still silent
        self.assertEqual(ml.overall(self.s), 1)
        ev = self.go(360, {DISK: 85.0, "memory": 90.0})
        self.assertEqual((kinds(ev), sorted(ev[0]["checks"])), (["reminder"], [DISK, "memory"]))


class Recovery(Machine):
    def open_memory(self):
        self.deliver(self.go(0, {"memory": 90.0}) + self.go(5, {"memory": 90.0}), 5)
        self.assertEqual(ml.overall(self.s), 1)

    def test_the_clear_margin_keeps_the_level_until_the_value_is_well_below(self):
        self.open_memory()
        for i in range(2, 12):  # 83% is below warn (85) but inside the 5 point margin
            self.assertEqual(self.go(i * 5, {"memory": 83.0}), [])
        self.assertEqual(ml.overall(self.s), 1)
        self.assertEqual(self.go(60, {"memory": 79.0}), [])  # first calm sample
        self.assertEqual(kinds(self.go(65, {"memory": 79.0})), ["recover"])  # recover_checks = 2
        self.assertEqual(ml.overall(self.s), 0)

    def test_disk_margin_is_two_points(self):
        self.go(0, {DISK: 85.0})
        self.go(5, {DISK: 78.0}), self.go(10, {DISK: 78.0}), self.go(15, {DISK: 78.0})  # 78 >= 80 - 2: still on
        self.assertEqual(ml.overall(self.s), 1)
        self.go(20, {DISK: 77.9})
        self.assertEqual(kinds(self.go(25, {DISK: 77.9})), ["recover"])

    def test_recovery_needs_consecutive_calm_samples(self):
        self.open_memory()
        self.go(10, {"memory": 70.0})
        self.go(15, {"memory": 83.0})  # back inside the margin: the count starts again
        self.assertEqual(self.go(20, {"memory": 70.0}), [])
        self.assertEqual(kinds(self.go(25, {"memory": 70.0})), ["recover"])

    def test_boolean_checks_clear_after_recover_checks_ok_samples_with_no_margin(self):
        self.go(0, {SVC: 1.0})
        self.assertEqual(self.go(5, {SVC: 0.0}), [])
        self.assertEqual(self.go(10, {SVC: 1.0}), [])  # down again: no second mail, count resets
        self.assertEqual(self.go(15, {SVC: 0.0}), [])
        self.assertEqual(kinds(self.go(20, {SVC: 0.0})), ["recover"])

    def test_the_recovery_event_summarises_the_incident(self):
        self.go(0, {DISK: 85.0, "failed_units": 0.0})
        self.go(5, {DISK: 92.0})
        self.go(10, {DISK: 50.0})
        ev = self.go(15, {DISK: 50.0})
        self.assertEqual(kinds(ev), ["recover"])
        inc = ev[0]["incident"]
        self.assertEqual((inc["peak"], inc["keys"]), (2, [DISK]))
        self.assertEqual(inc["opened"], at(0).isoformat(timespec="seconds"))
        self.assertEqual(ev[0]["checks"][DISK]["value"], 50.0)
        self.assertIsNone(self.s["incident"])

    def test_a_vanished_mount_drops_its_check_and_closes_the_incident(self):
        self.go(0, {"disk:/mnt/data": 95.0})
        ev = self.go(5, {DISK: 50.0})  # the mount is gone from the sample
        self.assertEqual(kinds(ev), ["recover"])
        self.assertNotIn("disk:/mnt/data", self.s["checks"])

    def test_an_unmeasured_value_changes_nothing(self):
        self.go(0, {DISK: 85.0})
        for m in range(5, 40, 5):
            self.assertEqual(self.go(m, {DISK: None}), [])
        self.assertEqual(ml.overall(self.s), 1)
        self.assertEqual(self.s["checks"][DISK]["down"], 0)
        self.assertEqual(self.go(45, {DISK: None, "cpu": None}), [])  # never measured: nothing recorded at all
        self.assertNotIn("cpu", self.s["checks"])


class Mails(Machine):
    def test_reminders_follow_the_last_delivered_mail_by_the_notify_interval(self):
        self.deliver(self.go(0, {DISK: 85.0}), 0)
        for m in range(5, 360, 5):
            self.assertEqual(self.go(m, {DISK: 85.0}), [], m)
        ev = self.go(360, {DISK: 85.0})
        self.assertEqual(kinds(ev), ["reminder"])
        self.deliver(ev, 360)
        self.assertEqual(self.go(365, {DISK: 85.0}), [])

    def test_a_day_of_steady_trouble_is_one_open_and_four_reminders(self):
        mails = []
        for m in range(0, 1441, 5):
            ev = self.go(m, {DISK: 85.0})
            mails += kinds(ev)
            self.deliver(ev, m)
        self.assertEqual(mails, ["open"] + ["reminder"] * 4)

    def test_flapping_inside_the_margin_sends_one_mail(self):
        mails = []
        for i, m in enumerate(range(0, 180, 5)):
            ev = self.go(m, {DISK: 81.0 if i % 2 == 0 else 79.0})
            mails += kinds(ev)
            self.deliver(ev, m)
        self.assertEqual(mails, ["open"])

    def test_no_reminder_is_stacked_while_a_mail_is_undelivered(self):
        self.go(0, {DISK: 85.0})  # never delivered
        for m in range(5, 800, 5):
            self.assertEqual(self.go(m, {DISK: 85.0}), [])
        self.assertEqual(kinds(ml.due(self.s, at(800))), ["open"])

    def test_a_failed_delivery_is_retried_every_fifteen_minutes(self):
        ev = self.go(0, {DISK: 85.0})
        self.assertEqual(kinds(ml.due(self.s, at(0))), ["open"])
        self.deliver(ev, 0, ok=False)
        self.assertEqual(ml.due(self.s, at(14)), [])
        due = ml.due(self.s, at(15))
        self.assertEqual((kinds(due), due[0]["tries"], due[0]["error"]), (["open"], 1, "refused"))
        self.deliver(due, 15)
        self.assertEqual(self.s["pending"], [])
        self.assertEqual(self.s["last_notify"], at(15).isoformat(timespec="seconds"))

    def test_a_newer_alert_replaces_an_undelivered_older_one(self):
        self.go(0, {DISK: 85.0})
        self.go(5, {DISK: 95.0})
        self.assertEqual(kinds(self.s["pending"]), ["escalate"])

    def test_recovery_replaces_undelivered_alerts_and_is_itself_retried(self):
        self.go(0, {DISK: 85.0})
        self.go(5, {DISK: 50.0})
        ev = self.go(10, {DISK: 50.0})
        self.assertEqual(kinds(ev), ["recover"])
        self.assertEqual(kinds(self.s["pending"]), ["recover"])

    def test_a_new_incident_after_an_undelivered_recovery_keeps_both(self):
        self.go(0, {DISK: 85.0}), self.go(5, {DISK: 50.0}), self.go(10, {DISK: 50.0})
        self.go(15, {DISK: 85.0})
        self.assertEqual(kinds(self.s["pending"]), ["recover", "open"])

    def test_weekly_summary_waits_a_full_period_and_can_be_switched_off(self):
        week = 7 * 24 * 60
        self.assertEqual(self.go(0, {"cpu": 5.0}), [])
        self.assertEqual(self.go(week - 5, {"cpu": 5.0}), [])
        self.assertEqual(kinds(self.go(week, {"cpu": 5.0})), ["summary"])
        self.assertEqual(self.go(week + 5, {"cpu": 5.0}), [])
        self.cfg = cfgof(summary_every_days=0)
        self.assertEqual(self.go(3 * week, {"cpu": 5.0}), [])


class Purity(Machine):
    def test_evaluate_does_not_touch_its_input_and_state_is_json(self):
        self.go(0, {DISK: 85.0})
        before = copy.deepcopy(self.s)
        ml.evaluate(self.s, {DISK: 50.0}, self.cfg, at(5))
        self.assertEqual(self.s, before)
        self.assertEqual(json.loads(json.dumps(self.s)), self.s)

    def test_a_state_from_nowhere_is_a_fresh_one(self):
        self.assertEqual(ml.new_state()["checks"], {})
        self.assertEqual(ml.overall(ml.new_state()), 0)


class Config(unittest.TestCase):
    def problems(self, **over):
        return ml.check(cfgof(**over))

    def test_defaults_are_valid(self):
        self.assertEqual(self.problems(), [])

    def test_thresholds_are_percent_and_warn_is_below_crit(self):
        self.assertTrue(self.problems(thresholds={"disk": {"warn": 90, "crit": 90}}))
        self.assertTrue(self.problems(thresholds={"cpu": {"crit": 101}}))
        self.assertTrue(self.problems(thresholds={"memory": {"warn": 0}}))
        self.assertTrue(self.problems(thresholds={"swap": {"sustain": 0}}))
        self.assertEqual(self.problems(thresholds={"disk": {"warn": 70, "crit": 100}}), [])

    def test_intervals_and_lists_are_checked(self):
        self.assertTrue(self.problems(check_interval_sec=5))
        self.assertTrue(self.problems(check_interval_sec=99999))
        self.assertTrue(self.problems(recover_checks=0))
        self.assertTrue(self.problems(notify_interval_hours=0))
        self.assertTrue(self.problems(summary_every_days=-1))
        self.assertTrue(self.problems(services=["nginx.service; rm -rf /"]))
        self.assertTrue(self.problems(history={"max_mb": 0}))
        self.assertTrue(self.problems(history={"retention_days": 0}))
        self.assertEqual(self.problems(services=["nginx.service", "postgresql@16-main.service"], summary_every_days=0), [])


if __name__ == "__main__":
    unittest.main()
