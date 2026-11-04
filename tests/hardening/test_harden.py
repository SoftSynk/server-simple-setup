"""synk-harden: lockout guard, firewall order, timed revert, rollback, confirmation. Everything runs against a
scripted machine (hardtest.Machine) over a temp tree; nothing here touches sshd, ufw or systemd."""
import contextlib
import json
import unittest
import unittest.mock as mock
from pathlib import Path

from hardtest import HOST, JOURNAL_PW, SS, SS6, STAMP, SYNK, UNIT, Base, core, harden


class Guard(Base):
    def test_refuses_without_a_publickey_login(self):
        self.m.journal = ""
        rc, out, err = self.apply("ssh_password")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("no entry for this session", err)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.m.ran("systemd-run"), [])
        self.assertEqual(self.m.ran("systemctl try-reload"), [])
        self.assertIsNone(self.pending())

    def test_refuses_a_password_login(self):
        self.m.journal = JOURNAL_PW
        rc, out, err = self.apply("ssh_password")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("logged in with password", err)
        self.assertFalse(self.dropin.exists())

    def test_refuses_a_key_login_of_another_connection(self):
        self.m.journal = "Accepted publickey for root from 203.0.113.9 port 40000 ssh2: ED25519 SHA256:abc\n"  # not this port
        rc, out, err = self.apply("ssh_password")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("no entry for this session", err)

    def test_refuses_on_the_console(self):
        self.session = None
        rc, out, err = self.apply("ssh_password")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("no live SSH connection", err)
        self.assertFalse(self.dropin.exists())

    def test_refuses_when_no_key_is_installed(self):
        (self.fs / "home/root/.ssh/authorized_keys").write_text("# nothing\n\n")
        rc, out, err = self.apply("ssh_password")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("no keys", err)

    def test_authorized_keys_command_counts_as_a_key_source(self):
        (self.fs / "home/root/.ssh/authorized_keys").write_text("")
        self.m.sshd["authorizedkeyscommand"] = "/usr/local/bin/fetch-keys %u"
        self.assertEqual(self.apply("ssh_password")[0], 0)

    def test_refuses_custom_authentication_methods(self):
        self.m.sshd["authenticationmethods"] = "publickey,password"
        rc, out, err = self.apply("ssh_password")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("AuthenticationMethods", err)

    def test_refuses_when_pubkey_login_is_off(self):
        self.m.sshd["pubkeyauthentication"] = "no"
        rc, out, err = self.apply("ssh_password")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("PubkeyAuthentication", err)

    def test_key_login_allows_password_off(self):
        self.assertEqual(self.apply("ssh_password")[0], 0)
        text = self.text(self.dropin)
        self.assertIn("PasswordAuthentication no\n", text)
        self.assertIn("KbdInteractiveAuthentication no\n", text)  # PAM would otherwise still take passwords

    def test_root_session_cannot_switch_root_login_off(self):
        env = {**self.only("ssh_rootlogin"), "SOFTSYNK_ANSWER_FINDING_SSH_ROOTLOGIN": "edit", "SOFTSYNK_ANSWER_VALUE_SSH_ROOTLOGIN": "no"}
        rc, out, err = self.cli("apply", tty=True, env=env)
        self.assertEqual(rc, core.FAILED)
        self.assertIn("logged in as root", err)
        self.assertFalse(self.dropin.exists())

    def test_root_login_by_key_stays_possible(self):
        self.assertEqual(self.apply("ssh_rootlogin")[0], 0)
        self.assertIn("PermitRootLogin prohibit-password", self.text(self.dropin))

    def test_root_login_change_needs_the_same_key_proof(self):
        self.m.journal = ""
        rc, out, err = self.apply("ssh_rootlogin")
        self.assertEqual(rc, core.FAILED)
        self.assertFalse(self.dropin.exists())

    def test_a_refusal_does_not_block_the_other_findings(self):
        self.m.journal = ""
        rc, out, err = self.apply("ssh_password", "ssh_x11")
        self.assertEqual(rc, core.FAILED)  # something asked for was not done
        self.assertEqual(self.text(self.dropin).count("no\n"), 1)
        self.assertIn("X11Forwarding no", self.text(self.dropin))
        self.assertNotIn("PasswordAuthentication", self.text(self.dropin))


class Firewall(Base):
    def test_ssh_port_is_allowed_before_ufw_is_enabled(self):
        self.assertEqual(self.apply("ufw_enable")[0], 0)
        allow, enable = self.m.ran("ufw allow 22/tcp"), self.m.ran("ufw --force enable")
        self.assertTrue(allow and enable)
        self.assertLess(allow[0], enable[0])
        self.assertLess(self.m.ran("ufw default deny incoming")[0], enable[0])
        self.assertTrue(self.m.active)

    def test_every_ssh_port_is_allowed_first(self):
        self.m.sshd["port"] = ["22", "2222"]
        self.assertEqual(self.apply("ufw_enable")[0], 0)
        self.assertLess(self.m.ran("ufw allow 2222/tcp")[0], self.m.ran("ufw --force enable")[0])
        self.assertLess(self.m.ran("ufw allow 22/tcp")[0], self.m.ran("ufw --force enable")[0])

    def test_the_port_of_this_connection_is_allowed_too(self):
        self.session = {"SSH_CONNECTION": "203.0.113.9 51234 192.0.2.10 2200"}
        self.m.estab = [("203.0.113.9", 51234, 2200)]
        self.m.journal = "Accepted publickey for root from 203.0.113.9 port 51234 ssh2: ED25519 SHA256:abc\n"
        self.assertEqual(self.apply("ufw_enable")[0], 0)
        self.assertTrue(self.m.ran("ufw allow 2200/tcp"))

    def test_public_web_ports_are_kept_open_and_databases_are_not(self):
        self.assertEqual(self.apply("ufw_enable")[0], 0)
        self.assertTrue(self.m.ran("ufw allow 80/tcp"))
        self.assertFalse(self.m.ran("ufw allow 3306"))
        self.assertFalse(self.m.ran("ufw allow 6379"))

    def test_ports_can_be_edited_and_are_checked(self):
        env = {**self.only("ufw_enable"), "SOFTSYNK_ANSWER_FINDING_UFW_ENABLE": "edit"}
        rc, out, err = self.cli("apply", tty=True, env={**env, "SOFTSYNK_ANSWER_VALUE_UFW_ENABLE": "443/tcp, 8080"})
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.m.ran("ufw allow 443/tcp") and self.m.ran("ufw allow 8080"))
        self.assertFalse(self.m.ran("ufw allow 80/tcp"))
        self.assertEqual(self.cli("revert")[0], 0)
        self.m.calls.clear()
        rc, out, err = self.cli("apply", tty=True, env={**env, "SOFTSYNK_ANSWER_VALUE_UFW_ENABLE": "80; rm -rf /"})
        self.assertIn("is not a port", err)  # asked again, then the proposed list is used: the typed text never reaches ufw
        self.assertFalse([c for c in self.m.calls if c[0] == "ufw" and any(";" in w or "rm" in w for w in c)])

    def test_without_ufw_nothing_is_offered(self):
        self.tools.clear()
        rc, out, err = self.cli("audit")
        self.assertNotIn("Turn on the firewall", out + err)
        self.assertIn("ufw is not installed", err)

    def test_an_active_firewall_is_left_alone(self):
        self.m.set_ufw(True, [("22/tcp", "ALLOW")])
        rc, out, err = self.cli("audit")
        self.assertNotIn("Turn on the firewall", out + err)
        self.assertNotIn("no allow rule", err)

    def test_an_active_firewall_without_an_ssh_rule_is_reported(self):
        self.m.set_ufw(True, [("80/tcp", "ALLOW")])
        rc, out, err = self.cli("audit")
        self.assertRegex(err, r"no allow rule for SSH port 22")


class FirewallRules(Base):
    """While ufw is off, `ufw status` hides its rules; `ufw show added` shows them, and enabling would activate them."""
    RISKY = ("ufw deny 22/tcp", "ufw deny 22", "ufw reject 22/tcp", "ufw deny 20:30/tcp", "ufw deny 22,80/tcp", "ufw deny ssh",
             "ufw deny OpenSSH", "ufw deny from any to any port 22 proto tcp", "ufw deny proto tcp from any to any port 22",
             "ufw deny in on eth0 to any port 22 proto tcp", "ufw deny from 198.51.100.7", "ufw deny in on eth0 from any",
             "ufw deny proto tcp from any to any", "ufw deny in on eth0", "ufw reject from any to any app OpenSSH", "ufw deny 'unbalanced")
    FINE = ("ufw allow 22/tcp", "ufw limit 22/tcp", "ufw deny 3306/tcp", "ufw deny 2000:3000/tcp", "ufw deny 2222/tcp",
            "ufw deny from 198.51.100.7 to any port 3306", "ufw deny out 25/tcp", "ufw route deny from any to any port 22",
            "ufw allow 80/tcp comment 'deny 22'", "ufw allow from any to any port 22", "(None)",
            "Added user rules (see 'ufw status' for running firewall):", "")

    def test_which_rules_can_block_ssh(self):
        for rule in self.RISKY:
            self.assertTrue(harden.blocks_ssh(rule, [22]), rule)
        for rule in self.FINE:
            self.assertFalse(harden.blocks_ssh(rule, [22]), rule)
        self.assertTrue(harden.blocks_ssh("ufw deny 2222/tcp", [22, 2222]))  # any SSH port counts

    def test_a_deny_rule_on_the_ssh_port_stops_the_firewall(self):
        self.m.added = ["ufw allow 80/tcp", "ufw deny 22/tcp"]
        rc, out, err = self.apply("ufw_enable")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("ufw_enable not applied", err)
        self.assertIn("ufw deny 22/tcp", err)  # the rule is named
        self.assertIn("/usr/sbin/ufw delete deny 22/tcp", err)
        self.assertEqual(self.m.ran("ufw --force enable"), [])
        self.assertEqual(self.m.ran("systemd-run"), [])
        self.assertFalse(self.m.active)
        self.assertIsNone(self.pending())

    def test_a_rule_for_every_port_stops_the_firewall(self):
        self.m.added = ["ufw deny from 198.51.100.7"]
        rc, out, err = self.apply("ufw_enable")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("ufw deny from 198.51.100.7", err)
        self.assertFalse(self.m.active)

    def test_the_other_findings_still_go_ahead(self):
        self.m.added = ["ufw deny 22/tcp"]
        rc, out, err = self.apply("ufw_enable", "ssh_x11")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("X11Forwarding no", self.text(self.dropin))
        self.assertFalse(self.m.active)

    def test_rules_that_do_not_touch_ssh_are_fine(self):
        self.m.added = ["ufw deny 3306/tcp", "ufw allow 80/tcp"]
        self.assertEqual(self.apply("ufw_enable")[0], 0)
        self.assertTrue(self.m.active)

    def test_an_unreadable_rule_list_refuses(self):
        self.m.show_added_rc = 1
        rc, out, err = self.apply("ufw_enable")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("ufw show added", err)
        self.assertFalse(self.m.active)

    def test_an_active_firewall_is_not_asked_about_its_rules(self):
        self.m.set_ufw(True, [("22/tcp", "ALLOW")])
        self.cli("audit")
        self.assertEqual(self.m.ran("ufw show added"), [])


class Ipv6(Base):
    def conf(self, text):
        (self.fs / "etc/default/ufw").write_text(text)

    def test_refused_when_ufw_ignores_ipv6_and_sshd_listens_on_it(self):
        self.m.ss = SS + SS6
        for text in ("IPV6=no\n", 'DEFAULT_INPUT_POLICY="DROP"\n', "# IPV6=yes\n", "IPV6=yes-ish\n"):  # anything but yes, or no line
            self.conf(text)
            rc, out, err = self.apply("ufw_enable")
            self.assertEqual(rc, core.FAILED, text)
            self.assertIn("IPV6", err)
            self.assertIn("/etc/default/ufw", err)
        self.assertEqual(self.m.ran("ufw --force enable"), [])
        self.assertEqual(self.m.ran("systemd-run"), [])
        self.assertFalse(self.m.active)

    def test_allowed_when_ufw_handles_ipv6(self):
        self.m.ss = SS + SS6
        self.conf("IPV6=yes\n")
        self.assertEqual(self.apply("ufw_enable")[0], 0)
        self.assertTrue(self.m.active)

    def test_an_ipv6_session_alone_is_enough_to_refuse(self):
        self.session = {"SSH_CONNECTION": "2001:db8::2 51234 2001:db8::1 22"}
        self.m.estab = [("2001:db8::2", 51234, 22)]
        self.m.journal = "Accepted publickey for root from 2001:db8::2 port 51234 ssh2: ED25519 SHA256:abc\n"
        self.conf("IPV6=no\n")
        rc, out, err = self.apply("ufw_enable")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("IPV6", err)
        self.conf("IPV6=yes\n")
        self.assertEqual(self.apply("ufw_enable")[0], 0)

    def test_an_ipv4_only_server_does_not_care(self):
        self.conf("IPV6=no\n")
        self.assertEqual(self.apply("ufw_enable")[0], 0)
        self.assertTrue(self.m.active)


class Timer(Base):
    def test_revert_timer_is_armed_before_the_reload(self):
        self.assertEqual(self.apply()[0], 0)
        arm, reload_, allow = self.m.ran("systemd-run"), self.m.ran("systemctl try-reload-or-restart ssh.service"), self.m.ran("ufw allow")
        self.assertTrue(arm and reload_ and allow)
        self.assertLess(arm[0], reload_[0])
        self.assertLess(arm[0], allow[0])

    def test_timer_unit_and_command(self):
        self.assertEqual(self.apply("ssh_x11")[0], 0)
        call = self.m.calls[self.m.ran("systemd-run")[0]]
        self.assertEqual(call[call.index("--unit") + 1], UNIT)
        self.assertIn("--on-active=600s", call)
        self.assertIn("--timer-property=AccuracySec=1s", call)
        i = call.index("revert")
        self.assertEqual(call[i:i + 3], ["revert", "--unit", UNIT])
        self.assertTrue(call[0] == "systemd-run" and call[i - 1].endswith("synk-harden"), call)
        p = self.pending()
        self.assertEqual((p["state"], p["stamp"], p["unit"], p["minutes"]), ("pending", STAMP, UNIT, 10))
        self.assertEqual(p["revert_at"], "2026-11-04T12:10:00+00:00")
        self.assertIn(UNIT + ".timer", self.m.timers)

    def test_revert_minutes_comes_from_the_config(self):
        self.write_cfg({"revert_minutes": 3})
        self.assertEqual(self.apply("ssh_x11")[0], 0)
        self.assertIn("--on-active=180s", self.m.calls[self.m.ran("systemd-run")[0]])

    def test_a_silly_revert_window_is_rejected(self):
        for v in (0, 1, 61, "10"):
            self.write_cfg({"revert_minutes": v})
            self.assertEqual(self.apply("ssh_x11")[0], core.USAGE, v)
        self.assertFalse(self.dropin.exists())

    def test_nothing_changes_when_the_timer_cannot_be_armed(self):
        self.m.script["systemd-run"] = [1]
        rc, out, err = self.apply()
        self.assertEqual(rc, core.FAILED)
        self.assertIn("revert timer", err)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.m.ran("systemctl try-reload"), [])
        self.assertEqual(self.m.ran("ufw allow"), [])
        self.assertFalse(self.m.active)
        self.assertNotEqual((self.pending() or {}).get("state"), "pending")

    def test_output_tells_you_to_test_a_new_session(self):
        rc, out, err = self.apply("ssh_x11")
        self.assertIn("NEW", err)
        self.assertIn("/usr/local/bin/synk-harden confirm", err)
        self.assertIn("/usr/local/bin/synk-harden revert", err)
        self.assertIn("console", err)
        self.assertNotIn("~", err.replace("~~", ""))
        self.assertNotIn("sudo", err)


class Rollback(Base):
    def test_failed_sshd_test_restores_the_files(self):
        self.dropin.write_text("# earlier\nMaxAuthTries 5\n")
        before = self.dropin.read_text()
        self.m.reject_dropin = True
        rc, out, err = self.apply("ssh_x11")
        self.assertEqual(rc, core.FAILED)
        self.assertEqual(self.dropin.read_text(), before)
        self.assertEqual(self.m.timers, set())
        self.assertEqual(self.pending()["state"], "reverted")
        self.assertEqual(self.m.ran("ufw allow"), [])

    def test_failed_sshd_test_removes_a_drop_in_that_did_not_exist(self):
        self.m.reject_dropin = True
        self.assertEqual(self.apply()[0], core.FAILED)
        self.assertFalse(self.dropin.exists())
        self.assertFalse(self.m.active)
        self.assertEqual(self.m.ran("ufw --force enable"), [])

    def test_a_setting_that_does_not_take_effect_is_rolled_back(self):
        self.m.ignored = {"passwordauthentication"}  # an earlier line in sshd_config wins
        rc, out, err = self.apply("ssh_password")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("still reports", err)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.pending()["state"], "reverted")

    def test_a_missing_include_is_named_plainly(self):
        (self.fs / "etc/ssh/sshd_config").write_text("UsePAM yes\n# Include /etc/ssh/sshd_config.d/*.conf\n")
        self.m.ignored = {"x11forwarding"}  # sshd never read the drop-in
        rc, out, err = self.apply("ssh_x11")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("no Include", err)
        self.assertIn("Include /etc/ssh/sshd_config.d/*.conf", err)
        self.assertFalse(self.dropin.exists())  # the rollback is unchanged
        self.assertEqual(self.pending()["state"], "reverted")

    def test_an_include_that_is_there_is_not_blamed(self):
        for line in ("Include /etc/ssh/sshd_config.d/*.conf", "  include sshd_config.d/*.conf", "Include /etc/ssh/a.conf /etc/ssh/sshd_config.d/*.conf"):
            (self.fs / "etc/ssh/sshd_config").write_text("UsePAM yes\n" + line + "\n")
            self.m.ignored = {"x11forwarding"}
            rc, out, err = self.apply("ssh_x11")
            self.assertEqual(rc, core.FAILED, line)
            self.assertIn("still reports", err)
            self.assertNotIn("no Include", err, line)

    def test_a_failed_firewall_step_puts_everything_back(self):
        self.m.script["ufw --force enable"] = [1]
        rules = (self.fs / "etc/ufw/user.rules").read_text()
        rc, out, err = self.apply()
        self.assertEqual(rc, core.FAILED)
        self.assertFalse(self.dropin.exists())
        self.assertEqual((self.fs / "etc/ufw/user.rules").read_text(), rules)
        self.assertTrue(self.m.ran("ufw --force disable"))
        self.assertFalse(self.m.active)
        self.assertEqual(self.m.timers, set())
        self.assertEqual(self.pending()["state"], "reverted")

    def test_the_timer_stays_when_the_rollback_itself_fails(self):
        self.m.reject_dropin = True
        self.m.script["systemctl try-reload-or-restart"] = [1]
        rc, out, err = self.apply("ssh_x11")
        self.assertEqual(rc, core.FAILED)
        self.assertIn(UNIT + ".timer", self.m.timers)  # still armed: the safety net must not go before the way back works
        self.assertEqual(self.pending()["state"], "pending")


class Danger(Base):
    def test_accept_defaults_cannot_bypass_confirm_danger(self):
        self.typed = ""
        rc, out, err = self.cli("apply", "--accept-defaults", tty=True)
        self.assertEqual(rc, 0)
        self.assertIn("No changes", err)
        self.assertFalse(self.dropin.exists())
        for prefix in ("systemd-run", "ufw allow", "ufw --force enable", "systemctl try-reload"):
            self.assertEqual(self.m.ran(prefix), [], prefix)
        self.assertIsNone(self.pending())

    def test_accept_defaults_and_the_wrong_target_still_cannot(self):
        self.typed = "other-host"
        rc, out, err = self.cli("apply", "--accept-defaults", tty=True)
        self.assertEqual(rc, 0)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.m.ran("systemd-run"), [])

    def test_apply_without_a_person_at_the_keyboard_is_refused(self):
        for argv in (("apply",), ("apply", "--accept-defaults"), ("apply", "--dry-run", "--accept-defaults")):
            rc, out, err = self.cli(*argv)
            self.assertEqual(rc, core.USAGE, argv)
            self.assertIn("keyboard", err)
            self.assertIn("audit", err)
        self.assertFalse(self.dropin.exists())
        for prefix in ("systemd-run", "ufw", "systemctl", "sshd"):
            self.assertEqual(self.m.ran(prefix), [], prefix)
        self.assertIsNone(self.pending())

    def test_there_is_no_scripted_confirm_option(self):
        with self.assertRaises(SystemExit) as cm:
            self.cli("apply", "--accept-defaults", "--confirm", f"harden={HOST}")
        self.assertEqual(cm.exception.code, core.USAGE)
        self.assertFalse(self.dropin.exists())

    def test_typing_the_wrong_hostname_cancels(self):
        self.typed = "nope"
        rc, out, err = self.apply()
        self.assertEqual(rc, 0)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.m.ran("systemd-run"), [])
        self.assertIn("No changes", err)

    def test_dry_run_shows_the_diff_and_changes_nothing(self):
        rc, out, err = self.cli("apply", "--dry-run", "--accept-defaults", tty=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("+PasswordAuthentication no", out)
        self.assertIn("ufw allow 22/tcp", out + err)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.m.ran("systemd-run"), [])
        self.assertEqual(self.m.ran("ufw allow"), [])

    def test_only_root_may_change_things(self):
        for argv in (("apply", "--accept-defaults"), ("confirm",), ("revert",), ("audit",)):
            rc, out, err = self.cli(*argv, host=self.host(uid=1000))
            self.assertEqual(rc, core.FAILED, argv)
            self.assertIn("root", err)
        self.assertEqual(self.m.ran("systemd-run"), [])


class Apply(Base):
    def test_default_flow_writes_the_expected_dropin(self):
        self.assertEqual(self.apply()[0], 0)
        text = self.text(self.dropin)
        self.assertTrue(text.startswith("# Managed by softsynk-server"))
        for line in ("PasswordAuthentication no", "PermitRootLogin prohibit-password", "MaxAuthTries 4", "X11Forwarding no",
                     "LoginGraceTime 30", "ClientAliveInterval 300", "ClientAliveCountMax 2"):
            self.assertIn(line + "\n", text)
        self.assertEqual(oct(self.dropin.stat().st_mode & 0o777), "0o644")
        self.assertTrue(self.m.ran("systemctl try-reload-or-restart ssh.service"))
        self.assertFalse(self.m.ran("systemctl restart"))

    def test_every_finding_is_explained_with_benefit_and_risk(self):
        rc, out, err = self.apply()
        for label in ("Current:", "Proposed:", "Benefit:", "Risk:"):
            self.assertGreaterEqual(err.count(label), 7, label)

    def test_the_preview_is_shown_before_anything_is_written(self):
        rc, out, err = self.apply("ssh_x11")
        self.assertIn("+X11Forwarding no", out)

    def test_edited_values_are_used(self):
        env = {**self.only("ssh_maxauth"), "SOFTSYNK_ANSWER_FINDING_SSH_MAXAUTH": "edit", "SOFTSYNK_ANSWER_VALUE_SSH_MAXAUTH": "3"}
        self.assertEqual(self.cli("apply", tty=True, env=env)[0], 0)
        self.assertIn("MaxAuthTries 3\n", self.text(self.dropin))

    def test_a_value_cannot_smuggle_in_more_config(self):
        env = {**self.only("ssh_maxauth"), "SOFTSYNK_ANSWER_FINDING_SSH_MAXAUTH": "edit"}
        for bad in ("3\nPermitRootLogin yes", "3 4", "-1", "99", "x", "\u0663"):
            rc, out, err = self.cli("apply", tty=True, env={**env, "SOFTSYNK_ANSWER_VALUE_SSH_MAXAUTH": bad})
            self.assertEqual(rc, 0, (bad, err))
            self.assertIn("must be a whole number", err)  # the bad value is asked again, then the proposed 4 is used
            self.assertEqual(self.text(self.dropin).splitlines()[1:], ["MaxAuthTries 4"], bad)
            self.assertEqual(self.cli("revert")[0], 0)

    def test_max_auth_tries_cannot_go_below_three(self):
        for low in ("1", "2", "0"):
            with self.assertRaises(core.Fail, msg=low):
                harden.render_dropin({}, {"ssh_maxauth": low})
        self.assertIn("MaxAuthTries 3\n", harden.render_dropin({}, {"ssh_maxauth": "3"}))
        env = {**self.only("ssh_maxauth"), "SOFTSYNK_ANSWER_FINDING_SSH_MAXAUTH": "edit", "SOFTSYNK_ANSWER_VALUE_SSH_MAXAUTH": "2"}
        rc, out, err = self.cli("apply", tty=True, env=env)
        self.assertIn("from 3 to 10", err)
        self.assertNotIn("MaxAuthTries 2", self.text(self.dropin))

    def test_a_drop_in_with_lines_that_are_not_ours_is_refused(self):
        for line in ("AllowUsers deploy", "Match User x", "MaxAuthTries=4", "Port 2222"):
            self.dropin.write_text("# mine\nX11Forwarding no\n" + line + "\n")
            before = self.dropin.read_text()
            rc, out, err = self.apply("ssh_maxauth")
            self.assertEqual(rc, core.FAILED, line)
            self.assertIn(line, err)
            self.assertIn("20-local.conf", err)
            self.assertEqual(self.dropin.read_text(), before)
        self.assertEqual(self.m.ran("systemd-run"), [])
        self.assertIsNone(self.pending())

    def test_our_own_drop_in_is_read_back_whole(self):
        vals = {"ssh_password": "no", "ssh_rootlogin": "prohibit-password", "ssh_emptypw": "no", "ssh_maxauth": "4", "ssh_x11": "no",
                "ssh_grace": "30", "ssh_alive": "300"}
        self.dropin.write_text(harden.render_dropin({}, vals))
        self.assertEqual(harden.read_dropin(str(self.dropin)), vals)
        self.assertEqual(harden.read_dropin(str(self.dropin) + ".missing"), {})

    def test_render_refuses_a_bad_value_even_without_the_prompt(self):
        with self.assertRaises(core.Fail):
            harden.render_dropin({}, {"ssh_maxauth": "3\nPermitRootLogin yes"})
        with self.assertRaises(core.Fail):
            harden.render_dropin({}, {"nope": "x"})

    def test_a_rerun_does_nothing_and_keeps_earlier_choices(self):
        self.applied("ssh_x11")
        self.assertEqual(self.cli("confirm", host=self.host())[0], core.FAILED)  # same session: refused, still pending
        self.new_session()
        self.assertEqual(self.cli("confirm")[0], 0)
        rc, out, err = self.apply("ssh_maxauth")
        self.assertEqual(rc, 0, err)
        text = self.text(self.dropin)
        self.assertIn("X11Forwarding no", text)  # kept from the first run
        self.assertIn("MaxAuthTries 4", text)

    def test_nothing_to_do_when_the_server_is_already_hardened(self):
        m = self.m
        m.sshd.update(passwordauthentication="no", kbdinteractiveauthentication="no", permitrootlogin="prohibit-password",
                      maxauthtries="3", x11forwarding="no", logingracetime="20", clientaliveinterval="300")
        m.set_ufw(True, [("22/tcp", "ALLOW")])
        rc, out, err = self.cli("apply", "--accept-defaults", tty=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("Nothing to harden", err)
        self.assertEqual(m.ran("systemd-run"), [])

    def test_a_pending_change_blocks_a_new_one(self):
        self.applied("ssh_x11")
        rc, out, err = self.apply("ssh_maxauth")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("waits for confirmation", err)
        self.assertNotIn("MaxAuthTries", self.text(self.dropin))

    def test_a_running_operation_blocks_a_second(self):
        with core.lock(self.ctx):
            rc, out, err = self.apply("ssh_x11")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("another", err)
        self.assertFalse(self.dropin.exists())

    def test_mail_failure_does_not_stop_the_revert(self):
        def boom(root, subject, body):
            raise OSError("no mail")
        self.applied("ssh_x11")
        rc, out, err = self.cli("revert", "--unit", UNIT, host=self.host(send=boom))
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.dropin.exists())

    def test_match_blocks_are_flagged_as_not_evaluated(self):
        (self.fs / "etc/ssh/sshd_config").write_text("PasswordAuthentication yes\nMatch User deploy\n  PasswordAuthentication yes\n")
        self.assertIn("Match blocks", self.cli("audit")[2])


class Confirm(Base):
    def test_confirm_refuses_the_session_that_applied_the_change(self):
        self.applied("ssh_x11")
        rc, out, err = self.cli("confirm")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("NEW", err)
        self.assertEqual(self.pending()["state"], "pending")
        self.assertIn(UNIT + ".timer", self.m.timers)

    def test_confirm_needs_a_login_after_the_change(self):
        self.applied("ssh_x11")
        self.new_session(logged=False)
        rc, out, err = self.cli("confirm")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("no login", err)
        self.assertEqual(self.pending()["state"], "pending")

    def test_confirm_asks_the_journal_only_about_logins_since_the_change(self):
        self.applied("ssh_x11")
        self.new_session()
        self.cli("confirm")
        last = [c for c in self.m.calls if c[0] == "journalctl"][-1]
        self.assertIn("--since=2026-11-04 12:00:00 UTC", last)

    def test_confirm_stops_the_timer(self):
        self.applied("ssh_x11")
        self.new_session()
        rc, out, err = self.cli("confirm")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.pending()["state"], "confirmed")
        self.assertEqual(self.m.timers, set())
        self.assertIn("X11Forwarding no", self.text(self.dropin))

    def test_confirm_keeps_the_change_even_if_the_timer_will_not_stop(self):
        self.applied("ssh_x11")
        self.new_session()
        self.m.script["systemctl stop"] = [1]
        rc, out, err = self.cli("confirm")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.pending()["state"], "confirmed")  # recorded first, so a late timer finds nothing to undo
        self.assertEqual(self.cli("revert", "--unit", UNIT)[0], 0)
        self.assertIn("X11Forwarding no", self.text(self.dropin))

    def test_confirm_twice_is_fine(self):
        self.applied("ssh_x11")
        self.new_session()
        self.assertEqual(self.cli("confirm")[0], 0)
        self.assertEqual(self.cli("confirm")[0], 0)

    def test_confirm_without_a_pending_change(self):
        rc, out, err = self.cli("confirm")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("nothing is waiting", err)

    def test_confirm_after_the_revert_says_so(self):
        self.applied("ssh_x11")
        self.assertEqual(self.cli("revert")[0], 0)
        self.new_session()
        rc, out, err = self.cli("confirm")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("reverted", err)

    def test_confirm_from_the_console_is_refused(self):
        self.applied("ssh_x11")
        self.session = None
        rc, out, err = self.cli("confirm")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("NEW SSH session", err)
        self.assertIn("/usr/local/bin/synk-harden revert", err)
        self.assertEqual(self.pending()["state"], "pending")
        self.assertIn(UNIT + ".timer", self.m.timers)
        self.assertEqual(self.m.ran("systemctl stop"), [])

    def test_confirm_from_a_session_whose_connection_is_gone_is_refused(self):
        self.applied("ssh_x11")
        self.new_session(live=False)  # the login is in the log, but a tmux holds its old SSH_CONNECTION
        rc, out, err = self.cli("confirm")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("no live SSH connection", err)
        self.assertEqual(self.pending()["state"], "pending")
        self.assertIn(UNIT + ".timer", self.m.timers)


class LiveSession(Base):
    """A shell inside tmux or screen keeps the SSH_CONNECTION of the login that started it, long after that login closed."""

    def live(self, text=None, rc=0):
        self.m.estab_text = text
        if rc:
            self.m.script["ss -Htn"] = [rc]
        return harden.session_of(self.host())

    SESS = {"ip": "203.0.113.9", "port": 51234, "server_port": 22}

    def test_an_established_socket_makes_the_session_live(self):
        self.assertEqual(self.live(), self.SESS)
        self.assertEqual(self.live("0 0 192.0.2.10:22 203.0.113.9:51234\n"), self.SESS)
        self.assertEqual(self.live("ESTAB 0 0 192.0.2.10:22 203.0.113.9:51234\n"), self.SESS)  # a State column does not matter
        self.assertEqual(self.live("0 0 [::ffff:192.0.2.10]:22 [::ffff:203.0.113.9]:51234\n"), self.SESS)  # dual-stack sshd

    def test_ipv6_sockets_are_matched(self):
        self.session = {"SSH_CONNECTION": "2001:db8::2 51234 2001:db8::1 22"}
        want = {"ip": "2001:db8::2", "port": 51234, "server_port": 22}
        self.assertEqual(self.live("0 0 [2001:db8::1]:22 [2001:db8::2]:51234\n"), want)
        self.assertEqual(self.live("0 0 [2001:db8::1%eth0]:22 [2001:DB8:0::2]:51234\n"), want)
        self.assertIsNone(self.live("0 0 [2001:db8::1]:22 [2001:db8::3]:51234\n"))

    def test_a_closed_connection_is_not_a_session(self):
        for text in ("", "0 0 192.0.2.10:22 203.0.113.9:40000\n", "0 0 192.0.2.10:443 203.0.113.9:51234\n",
                     "0 0 192.0.2.10:22 198.51.100.4:51234\n", "garbage\n\n:\n x:y\n"):
            self.assertIsNone(self.live(text), text)

    def test_when_ss_fails_nothing_is_trusted(self):
        self.assertIsNone(self.live(rc=1))

    def test_a_stale_session_cannot_pass_the_key_proof(self):
        self.m.estab = []
        rc, out, err = self.apply("ssh_password")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("no live SSH connection", err)
        self.assertIn("tmux", err)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.m.ran("systemd-run"), [])

    def test_a_stale_session_does_not_add_its_port_to_the_firewall(self):
        self.session = {"SSH_CONNECTION": "203.0.113.9 51234 192.0.2.10 2200"}
        self.m.estab = []
        self.assertEqual(self.apply("ufw_enable")[0], 0)
        self.assertFalse(self.m.ran("ufw allow 2200/tcp"))
        self.assertTrue(self.m.ran("ufw allow 22/tcp"))


class Revert(Base):
    def test_the_timer_restores_files_firewall_and_ssh(self):
        rules = (self.fs / "etc/ufw/user.rules").read_text()
        self.applied()
        self.assertTrue(self.m.active and self.dropin.exists())
        self.m.calls.clear()
        rc, out, err = self.cli("revert", "--unit", UNIT)
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.dropin.exists())
        self.assertEqual((self.fs / "etc/ufw/user.rules").read_text(), rules)
        self.assertEqual((self.fs / "etc/ufw/ufw.conf").read_text(), "ENABLED=no\n")
        self.assertTrue(self.m.ran("ufw --force disable"))
        self.assertTrue(self.m.ran("systemctl try-reload-or-restart ssh.service"))
        self.assertFalse(self.m.active)
        self.assertEqual(self.m.timers, set())
        self.assertEqual(self.pending()["state"], "reverted")
        self.assertTrue(any("reverted" in s.lower() for s, b in self.sent))

    def test_a_late_timer_cannot_undo_a_confirmed_change(self):
        self.applied("ssh_x11")
        self.new_session()
        self.assertEqual(self.cli("confirm")[0], 0)
        before = self.text(self.dropin)
        self.m.calls.clear()
        rc, out, err = self.cli("revert", "--unit", UNIT)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.text(self.dropin), before)
        self.assertEqual(self.m.ran("systemctl try-reload"), [])

    def test_a_timer_of_an_older_change_is_ignored(self):
        self.applied("ssh_x11")
        before = self.text(self.dropin)
        self.assertEqual(self.cli("revert", "--unit", "softsynk-harden-revert-20200101-000000")[0], 0)
        self.assertEqual(self.text(self.dropin), before)
        self.assertEqual(self.pending()["state"], "pending")

    def test_revert_does_not_undo_a_confirmed_change(self):
        self.applied("ssh_x11")
        self.new_session()
        self.cli("confirm")
        rc, out, err = self.cli("revert")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("confirmed", err)
        self.assertTrue(self.dropin.exists())
        self.assertEqual(self.pending()["state"], "confirmed")

    def test_manual_revert_of_a_pending_change_needs_no_question(self):
        self.applied("ssh_x11")
        self.assertEqual(self.cli("revert")[0], 0)
        self.assertFalse(self.dropin.exists())

    def test_a_failed_reload_leaves_the_change_pending(self):
        self.applied("ssh_x11")
        self.m.script["systemctl try-reload-or-restart"] = [1]
        rc, out, err = self.cli("revert")
        self.assertEqual(rc, core.FAILED)
        self.assertEqual(self.pending()["state"], "pending")

    def test_revert_with_nothing_to_revert(self):
        rc, out, err = self.cli("revert")
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to revert", err)


class LockWait(Base):
    """The revert timer fires once. If a confirm or revert holds the lock at that moment, it must wait, not give up."""

    def test_the_timer_waits_for_a_busy_lock(self):
        self.applied("ssh_x11")
        held, naps = contextlib.ExitStack(), []
        held.enter_context(core.lock(self.ctx))
        self.addCleanup(held.close)

        def nap(seconds):
            naps.append(seconds)
            if len(naps) == 3:
                held.close()  # whoever held the lock is done

        with mock.patch.object(harden.time, "sleep", nap):
            rc, out, err = self.cli("revert", "--unit", UNIT)
        self.assertEqual((rc, naps), (0, [1, 1, 1]), err)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.pending()["state"], "reverted")

    def test_the_timer_gives_up_after_two_minutes(self):
        self.applied("ssh_x11")
        with core.lock(self.ctx), mock.patch.object(harden.time, "sleep") as nap:
            rc, out, err = self.cli("revert", "--unit", UNIT)
        self.assertEqual(rc, core.FAILED)
        self.assertIn("lock", err)
        self.assertEqual(nap.call_args_list, [mock.call(1)] * 120)
        self.assertTrue(self.dropin.exists())
        self.assertEqual(self.pending()["state"], "pending")

    def test_a_person_does_not_wait(self):
        self.applied("ssh_x11")
        for argv in (("revert",), ("confirm",), ("apply",)):
            with core.lock(self.ctx), mock.patch.object(harden.time, "sleep") as nap:
                rc, out, err = self.cli(*argv, tty=argv == ("apply",))
            self.assertEqual(rc, core.FAILED, argv)
            nap.assert_not_called()


class Status(Base):
    def status(self, **kw):
        rc, out, err = self.cli("--status-json", **kw)
        self.assertEqual(rc, 0)
        return json.loads(out)

    def test_fresh_server(self):
        s = self.status()
        self.assertEqual((s["module"], s["install_state"], s["health"]), ("hardening", "not_installed", "unknown"))
        self.assertIn("synk-harden apply", " ".join(s["actions"]))

    def test_pending_change(self):
        self.applied("ssh_x11")
        s = self.status()
        self.assertEqual((s["install_state"], s["health"]), ("installed", "degraded"))
        self.assertTrue(any("confirm" in w for w in s["warnings"]))

    def test_pending_change_whose_timer_is_gone(self):
        self.applied("ssh_x11")
        self.m.timers.clear()  # a reboot loses transient timers
        s = self.status()
        self.assertEqual(s["health"], "unhealthy")
        self.assertTrue(any("timer" in w for w in s["warnings"]))

    def test_confirmed(self):
        self.applied("ssh_x11")
        self.new_session()
        self.cli("confirm")
        s = self.status()
        self.assertEqual((s["install_state"], s["health"]), ("installed", "healthy"))
        self.assertIn("open", s["summary"])

    def test_reverted_change_is_reported(self):
        self.applied("ssh_x11")
        self.cli("revert")
        s = self.status()
        self.assertEqual(s["install_state"], "not_installed")
        self.assertTrue(any("reverted" in w for w in s["warnings"]))

    def test_status_always_answers(self):
        self.m.script["sshd -T"] = [1]
        s = self.status()
        self.assertEqual(s["schema"], 1)
        self.assertTrue(s["warnings"] or "cannot" in s["summary"])
        pending = Path(self.ctx.path("state", "pending.json"))
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text("{not json")
        self.assertEqual(self.status()["schema"], 1)


class Audit(Base):
    def test_audit_is_read_only(self):
        rc, out, err = self.cli("audit")
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.dropin.exists())
        self.assertFalse(Path(self.ctx.path("state", "pending.json")).exists())
        for prefix in ("systemd-run", "ufw allow", "ufw --force", "systemctl"):
            self.assertEqual(self.m.ran(prefix), [], prefix)
        for name in ("PasswordAuthentication", "X11Forwarding"):
            self.assertIn(name, out + err)

    def test_audit_lists_findings_with_benefit_and_risk(self):
        rc, out, err = self.cli("audit")
        self.assertEqual(rc, 0, err)
        titles = ["PasswordAuthentication", "PermitRootLogin", "MaxAuthTries", "X11Forwarding", "LoginGraceTime", "ClientAliveInterval", "Turn on the firewall"]
        at = [err.index(t) for t in titles]
        self.assertEqual(at, sorted(at))
        for label in ("Current:", "Proposed:", "Benefit:", "Risk:"):
            self.assertEqual(err.count(label), 7, label)


class Parts(Base):
    def test_ancestry_is_read_from_the_parents_when_sudo_dropped_the_session(self):
        proc = self.tmp / "proc"
        for pid, ppid, env in ((300, 200, "SUDO_USER=alice\0HOME=/root\0"), (200, 100, "SSH_CONNECTION=203.0.113.9 51234 192.0.2.10 22\0USER=alice\0"),
                               (100, 1, "TERM=xterm\0")):
            (proc / str(pid)).mkdir(parents=True)
            (proc / str(pid) / "status").write_text(f"Name:\tx\nPPid:\t{ppid}\nUid:\t0\t0\t0\t0\n")
            (proc / str(pid) / "environ").write_bytes(env.encode())
        envs = harden.read_ancestry(str(proc), 300)
        self.assertEqual(len(envs), 3)
        self.assertEqual(harden.find_session(envs), {"ip": "203.0.113.9", "port": 51234, "server_port": 22})

    def test_no_session_in_a_console_environment(self):
        self.assertIsNone(harden.find_session([{"HOME": "/root"}, {"SSH_CONNECTION": "garbage"}]))

    def test_module_toml_names_the_commands(self):
        import tomllib
        t = tomllib.loads((SYNK.parent / "module.toml").read_text())
        self.assertEqual((t["id"], t["schema"]), ("hardening", 1))
        self.assertEqual(t["operations"]["status"], ["synk-harden", "--status-json"])
        self.assertEqual(t["commands"], {"synk-harden": "synk-harden"})
        self.assertTrue((SYNK.parent / "synk-harden").stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
