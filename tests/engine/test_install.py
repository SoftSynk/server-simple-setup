"""install / configure: order, blocking, cancel, non-interactive input, lock, prompts."""
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

import support
from support import Base, engine, mark_installed, run_pty

AD = ["--accept-defaults"]
PLAIN_PATH = "/usr/bin:/bin"  # no gum here: forces the plain-prompt fallback


def order_log(root):
    p = Path(root) / "install-order.log"
    return p.read_text().split() if p.exists() else []


class InstallFlow(Base):
    def test_installs_in_dependency_order_whatever_the_argument_order(self):
        rel = self.release(["alpha", "beta", "zeta"])
        r = engine(rel, ["install", "zeta", "beta", "alpha", *AD], self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(order_log(self.root), ["alpha", "beta", "zeta"])
        self.assertIn("Plan:", r.stdout)

    def test_env_contract_reaches_the_module(self):
        rel = self.release(["alpha"])
        r = engine(rel, ["install", "alpha", "--dry-run", *AD], self.root,
                   {"SOFTSYNK_LOCK_HELD": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        env = dict(line.split("=", 1) for line in (self.root / "env-alpha.txt").read_text().splitlines())
        self.assertEqual(env["SOFTSYNK_ROOT"], str(self.root))
        self.assertEqual(env["SOFTSYNK_MODULE"], "alpha")
        for key, sub in (("ETC", "etc"), ("SECRETS", "secrets"), ("STATE", "state"), ("LOG", "log"), ("TMP", "tmp")):
            self.assertEqual(env[f"SOFTSYNK_{key}_DIR"], str(self.root / sub / "alpha"))
        self.assertEqual((env["SOFTSYNK_INTERACTIVE"], env["SOFTSYNK_NONINTERACTIVE"]), ("0", "1"))
        self.assertEqual((env["SOFTSYNK_ACCEPT_DEFAULTS"], env["SOFTSYNK_DRY_RUN"]), ("1", "1"))
        self.assertTrue(env["SOFTSYNK_RUN_ID"])
        self.assertNotIn("SOFTSYNK_LOCK_HELD", env)

    def test_failed_install_blocks_dependents_and_independents_still_run(self):
        rel = self.release(["alpha", "afterfail", "failinstall"])
        r = engine(rel, ["install", "afterfail", "failinstall", "alpha", *AD], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("failinstall: failed", r.stdout)
        self.assertIn("afterfail: blocked (requires failinstall, which failed)", r.stdout)
        self.assertEqual(order_log(self.root), ["alpha"])

    def test_requires_not_installed_and_not_selected_is_blocked_exit_3(self):
        rel = self.release(["alpha", "beta"])
        r = engine(rel, ["install", "beta", *AD], self.root)
        self.assertEqual(r.returncode, 3)
        self.assertIn("beta: BLOCKED, requires alpha, which is not installed", r.stdout)
        self.assertEqual(order_log(self.root), [])

    def test_requires_already_installed_is_satisfied(self):
        rel = self.release(["alpha", "beta"])
        mark_installed(self.root, "alpha")
        r = engine(rel, ["install", "beta", *AD], self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(order_log(self.root), ["beta"])

    def test_invalid_manifest_is_blocked_not_run(self):
        rel = self.release(["bad-shell-string", "needs-bad"])
        r = engine(rel, ["install", "bad-shell-string", "needs-bad", *AD], self.root)
        self.assertEqual(r.returncode, 3)
        self.assertIn("invalid manifest", r.stdout)
        self.assertEqual(order_log(self.root), [])

    def test_cancelled_module_gives_130_and_independents_continue(self):
        rel = self.release(["cancelinstall", "alpha"])
        r = engine(rel, ["install", "cancelinstall", "alpha", *AD], self.root)
        self.assertEqual(r.returncode, 130)
        self.assertIn("cancelinstall: cancelled", r.stdout)
        self.assertEqual(order_log(self.root), ["alpha"])

    def test_unsupported_platform_is_blocked(self):
        rel = self.release(["ubuntuonly"])
        r = engine(rel, ["install", "ubuntuonly", *AD], self.root,
                   {"SOFTSYNK_OS_RELEASE": support.OS_RELEASE["debian-12"]})
        self.assertEqual(r.returncode, 3)
        self.assertIn("unsupported platform", r.stdout)

    def test_history_is_recorded(self):
        rel = self.release(["alpha", "failinstall"])
        engine(rel, ["install", "alpha", "failinstall", *AD], self.root)
        lines = (self.root / "state" / "framework" / "history.jsonl").read_text().splitlines()
        entry = json.loads(lines[-1])
        self.assertEqual((entry["command"], entry["exit"], entry["version"]), ("install", 2, "0.1.0"))
        self.assertEqual(entry["results"]["alpha"]["state"], "ok")
        self.assertEqual(entry["results"]["failinstall"]["state"], "failed")


class Configure(Base):
    def test_runs_configure_with_context(self):
        rel = self.release(["alpha"])
        r = engine(rel, ["configure", "alpha", "--dry-run", *AD], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.root / "etc" / "alpha" / "configured").read_text().strip(),
                         "interactive=0 accept=1 dry=1")

    def test_module_without_configure_is_a_usage_error(self):
        rel = self.release(["gamma"])
        r = engine(rel, ["configure", "gamma"], self.root)
        self.assertEqual(r.returncode, 64)
        self.assertIn("does not declare a configure", r.stderr)

    def test_unusable_module_exits_3(self):
        rel = self.release(["bad-shell-string"])
        r = engine(rel, ["configure", "bad-shell-string"], self.root)
        self.assertEqual(r.returncode, 3)


class NonInteractive(Base):
    """Missing required input must exit 64, never hang or guess."""

    def setUp(self):
        super().setUp()
        self.rel = self.release(["alpha", "beta"])

    def assert_usage(self, args, text=None):
        r = engine(self.rel, args, self.root, timeout=20)
        self.assertEqual(r.returncode, 64, r.stdout + r.stderr)
        if text:
            self.assertIn(text, r.stderr)
        self.assertFalse((self.root / "install-order.log").exists())

    def test_install_without_names(self):
        self.assert_usage(["install"], "install needs module names")

    def test_configure_without_module(self):
        self.assert_usage(["configure"], "configure needs a module name")

    def test_install_without_accept_defaults(self):
        self.assert_usage(["install", "alpha"], "re-run with --accept-defaults")

    def test_unknown_module(self):
        self.assert_usage(["install", "nosuch", *AD], "unknown module(s): nosuch")
        self.assert_usage(["configure", "nosuch", *AD], "unknown module: nosuch")

    def test_bare_command_without_a_terminal(self):
        self.assert_usage([], "no command given")

    def test_update_inputs(self):
        self.assert_usage(["update"], "update needs --from")
        self.assert_usage(["update", "--from", str(self.rel)], "confirmation required")
        self.assert_usage(["update", "--from", str(self.rel), "--sha256", support.ZERO_SHA],
                          "applies to tarball sources only")

    def test_bad_flag_combinations(self):
        self.assert_usage(["--json"], "--json is only valid with --status")
        self.assert_usage(["--status", "--list"], "choose one action")
        self.assert_usage(["--dry-run", "--status"], "--dry-run is only valid")
        self.assert_usage(["alpha"], "invalid choice")
        self.assert_usage(["--no-such-flag"])
        self.assert_usage(["status", "x"])

    def test_bad_timeout_env(self):
        r = engine(self.rel, ["--status"], self.root, {"SOFTSYNK_STATUS_TIMEOUT": "abc"})
        self.assertEqual(r.returncode, 64)

    def test_env_switch_forces_non_interactive(self):
        r = engine(self.rel, ["install", "alpha"], self.root, {"SOFTSYNK_NONINTERACTIVE": "1"})
        self.assertEqual(r.returncode, 64)



class CtrlC(Base):
    def test_sigint_during_a_module_cancels_it_not_the_engine(self):
        import signal
        rel = self.release(["sigmod", "alpha"])
        p = subprocess.Popen([sys.executable, str(rel / "softsynk-server"), "--root", str(self.root),
                              "install", "sigmod", "alpha", *AD], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        deadline = time.time() + 20
        while not (self.root / "hold.marker").exists():
            self.assertLess(time.time(), deadline, "module never started")
            time.sleep(0.05)
        os.killpg(p.pid, signal.SIGINT)  # what a terminal does on Ctrl-C: signal the whole foreground group
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130, out + err)
        self.assertIn("sigmod: cancelled", out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(order_log(self.root), ["alpha"])  # the independent module still ran


class Lock(Base):
    def test_second_mutating_command_is_refused_while_the_first_runs(self):
        rel = self.release(["holdinstall", "alpha"])
        first = subprocess.Popen([sys.executable, str(rel / "softsynk-server"), "--root", str(self.root),
                                  "install", "holdinstall", *AD], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 20
            while not (self.root / "hold.marker").exists():
                self.assertLess(time.time(), deadline, "first command never started its module")
                time.sleep(0.05)
            second = engine(rel, ["install", "alpha", *AD], self.root)
            self.assertEqual(second.returncode, 2, second.stdout)
            self.assertIn("another softsynk-server command is running", second.stderr)
            configure = engine(rel, ["configure", "alpha", *AD], self.root)
            self.assertEqual(configure.returncode, 2)
            status = engine(rel, ["--status"], self.root)  # read-only commands do not need the lock
            self.assertEqual(status.returncode, 0, status.stdout)
        finally:
            first.communicate(timeout=30)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(order_log(self.root), ["holdinstall"])  # the refused one installed nothing
        third = engine(rel, ["install", "alpha", *AD], self.root)  # lock was released
        self.assertEqual(third.returncode, 0)


@unittest.skipUnless(shutil.which("gum", path=PLAIN_PATH) is None, "gum found on the minimal PATH")
class InteractivePlain(Base):
    """Terminal prompts without gum (pty)."""

    def env(self):
        return {"PATH": PLAIN_PATH}

    def test_declining_a_dependency_blocks_its_dependent(self):
        rel = self.release(["alpha", "beta"])
        code, out = run_pty(rel, ["install"], self.root, ["n", "y"], self.env())
        self.assertEqual(code, 3, out)
        self.assertIn("beta: BLOCKED, requires alpha, which is not installed and was not selected", out)
        self.assertEqual(order_log(self.root), [])

    def test_accepting_everything_installs_in_order(self):
        rel = self.release(["beta", "alpha"])
        code, out = run_pty(rel, ["install"], self.root, ["y", "y", "y"], self.env())
        self.assertEqual(code, 0, out)
        self.assertEqual(order_log(self.root), ["alpha", "beta"])
        env = (self.root / "env-alpha.txt").read_text()
        self.assertIn("SOFTSYNK_INTERACTIVE=1", env)

    def test_declining_the_plan_changes_nothing(self):
        rel = self.release(["alpha"])
        code, out = run_pty(rel, ["install"], self.root, ["y", "n"], self.env())
        self.assertEqual(code, 0, out)
        self.assertIn("aborted, nothing changed", out)
        self.assertEqual(order_log(self.root), [])

    def test_end_of_input_is_a_cancel(self):
        rel = self.release(["alpha"])
        code, out = run_pty(rel, ["install"], self.root, [support.EOF], self.env())
        self.assertEqual(code, 130, out)
        self.assertEqual(order_log(self.root), [])

    def test_menu_runs_status_and_quits(self):
        rel = self.release(["alpha"])
        code, out = run_pty(rel, [], self.root, ["1", "7"], self.env())
        self.assertEqual(code, 0, out)
        self.assertIn("overall:", out)


class InteractiveGumStub(Base):
    """The gum code path, with a stub that records how it was called."""

    def stub(self, body):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        gum = bin_dir / "gum"
        gum.write_text("#!/bin/sh\n" + body)
        gum.chmod(gum.stat().st_mode | stat.S_IEXEC)
        return {"PATH": f"{bin_dir}:{PLAIN_PATH}", "GUM_LOG": str(self.tmp / "gum.log")}

    def test_ctrl_c_in_a_prompt_cancels_with_130(self):
        env = self.stub("exit 130\n")
        rel = self.release(["alpha"])
        code, out = run_pty(rel, ["install"], self.root, [], env)
        self.assertEqual(code, 130, out)
        self.assertEqual(order_log(self.root), [])

    def test_only_basic_gum_flags_are_used(self):
        # choose: first call picks Status, then Quit; confirm answers yes
        env = self.stub('echo "$@" >> "$GUM_LOG"\n'
                        'case "$1" in\n'
                        '  choose) n=$(cat "$GUM_LOG.n" 2>/dev/null || echo 0); echo $((n+1)) > "$GUM_LOG.n"\n'
                        '          if [ "$n" = 0 ]; then echo Status; else echo Quit; fi ;;\n'
                        '  confirm) exit 0 ;;\n'
                        '  input) echo x ;;\n'
                        'esac\n')
        rel = self.release(["alpha"])
        code, out = run_pty(rel, [], self.root, [], env)
        self.assertEqual(code, 0, out)
        calls = (self.tmp / "gum.log").read_text().splitlines()
        self.assertEqual(len(calls), 2)
        for call in calls:
            words = call.split()
            self.assertEqual(words[0], "choose")
            self.assertIn("--header", words)
            self.assertTrue({w for w in words if w.startswith("-")} <= {"--header", "--no-limit"}, call)


if __name__ == "__main__":
    unittest.main()
