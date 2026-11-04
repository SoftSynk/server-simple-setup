"""ui: the prompt rules of lib/ui.sh in Python (answers, non-interactive, defaults, danger), review, preview, output."""
import io
import os
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from libtest import Base, wr

from softsynk import core, ui

GUM_STUB = """#!/bin/sh
case "$1" in
  --version) echo "gum version ${GUM_VERSION:-0.14.3}"; exit 0 ;;
esac
[ -n "$GUM_RC" ] && exit "$GUM_RC"
case "$1" in
  input) echo "from-gum" ;;
  choose) shift; while [ "$#" -gt 0 ]; do case "$1" in --*) shift 2 ;; *) echo "$1"; break ;; esac; done ;;
esac
"""


class UiBase(Base):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if not k.startswith("SOFTSYNK_") and k != "NO_COLOR"}
        env["SOFTSYNK_NONINTERACTIVE"] = "1"
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        ui.setup()
        self.addCleanup(ui.setup)
        self.addCleanup(core._SECRETS.clear)

    def answer(self, name, value):
        os.environ["SOFTSYNK_ANSWER_" + name] = value

    def accept_defaults(self):
        ui.setup(types.SimpleNamespace(accept_defaults=True))

    def enter(self, cm):
        """Enter a context manager for the rest of the test."""
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def typing(self, text):
        """A person at a terminal without gum: plain prompts read `text` from stdin."""
        self.enter(mock.patch.object(ui, "interactive", return_value=True))
        self.enter(mock.patch.object(ui, "_gum_ok", return_value=False))
        self.enter(mock.patch.object(sys, "stdin", io.StringIO(text)))
        self.enter(redirect_stderr(io.StringIO()))

    def usage_error(self, fn, *a):
        with self.assertRaises(core.Fail) as c:
            fn(*a)
        self.assertEqual(c.exception.code, core.USAGE)
        return str(c.exception)


class Answers(UiBase):
    def test_answer_env_is_consumed_once_and_empty_falls_back_to_the_default(self):
        self.answer("HOST", "mail.example.com")
        self.assertEqual(ui.ask("HOST", "Host?", "x"), "mail.example.com")
        self.assertNotIn("SOFTSYNK_ANSWER_HOST", os.environ)  # a retry cannot reuse it
        self.usage_error(ui.ask, "HOST", "Host?", "x")
        self.answer("HOST", "")
        self.assertEqual(ui.ask("HOST", "Host?", "dflt"), "dflt")

    def test_non_interactive_missing_answer_names_the_option_and_exits_64(self):
        msg = self.usage_error(ui.ask, "SMTP_HOST", "SMTP host?")
        self.assertIn("--smtp-host", msg)
        self.assertIn("SOFTSYNK_ANSWER_SMTP_HOST", msg)
        self.assertIn("no default", msg)
        msg = self.usage_error(ui.ask, "SMTP_HOST", "SMTP host?", "localhost")
        self.assertIn("--accept-defaults", msg)
        self.usage_error(ui.confirm, "Proceed?", True)
        self.assertIn("--smtp-host", self.usage_error(ui.choose, "SMTP_HOST", "Which?", ["a", "b"]))

    def test_accept_defaults_takes_each_default_but_never_invents_one(self):
        for how in ("setup", "env"):
            with self.subTest(how):
                if how == "setup":
                    self.accept_defaults()
                else:
                    ui.setup()
                    os.environ["SOFTSYNK_ACCEPT_DEFAULTS"] = "1"
                self.assertEqual(ui.ask("HOST", "Host?", "localhost"), "localhost")
                self.assertIs(ui.confirm("Proceed?", True), True)
                self.assertIs(ui.confirm("Proceed?"), False)
                self.assertEqual(ui.choose("MODE", "Mode?", ["a", "b"], "b"), "b")
                self.assertEqual(ui.choose("MODE", "Mode?", ["a", "b"]), "a")
                self.usage_error(ui.ask, "HOST", "Host?")  # no default: still needs an answer

    def test_choose_matches_index_or_first_word_case_insensitively(self):
        opts = ["apply this", "skip it", "edit value"]
        self.answer("PICK", "2")
        self.assertEqual(ui.choose("PICK", "?", opts), "skip it")
        self.answer("PICK", "EDIT")
        self.assertEqual(ui.choose("PICK", "?", opts), "edit value")
        self.answer("PICK", "9")
        self.assertIn("Invalid value for --pick", self.usage_error(ui.choose, "PICK", "?", opts))
        with self.assertRaises(ValueError):
            ui.choose("PICK", "?", opts, "nope")

    def test_password_comes_only_from_the_environment_and_is_then_redacted(self):
        self.answer("SMTP_PASSWORD", "s3cretvalue")
        self.assertEqual(ui.password("SMTP_PASSWORD", "Password?"), "s3cretvalue")
        self.assertNotIn("SOFTSYNK_ANSWER_SMTP_PASSWORD", os.environ)
        self.assertEqual(core.redact("pw=s3cretvalue"), "pw=***")
        self.accept_defaults()
        self.assertIn("secret", self.usage_error(ui.password, "SMTP_PASSWORD", "Password?"))  # defaults never apply

    def test_confirm_danger_is_never_satisfied_by_defaults(self):
        self.accept_defaults()
        self.assertIn("--confirm ufw-enable=host1", self.usage_error(ui.confirm_danger, "ufw-enable", "host1"))
        ui.setup(types.SimpleNamespace(accept_defaults=True, confirm=["ufw-enable=host1"]))
        self.assertTrue(ui.confirm_danger("ufw-enable", "host1"))
        self.usage_error(ui.confirm_danger, "ufw-enable", "host2")  # a confirmation names one target
        self.usage_error(ui.confirm_danger, "other", "host1")

    def test_confirm_arg_feeds_setup(self):
        p = core.Parser(prog="t")
        ui.confirm_arg(p)
        sub = p.add_subparsers(dest="cmd").add_parser("go")
        ui.confirm_arg(sub)
        ui.setup(core.parse_args(p, ["--confirm", "a=b", "--confirm", "e=f", "go"]))
        self.assertTrue(ui.confirm_danger("a", "b") and ui.confirm_danger("e", "f"))
        ui.setup(core.parse_args(p, ["go", "--confirm", "c=d"]))
        self.assertTrue(ui.confirm_danger("c", "d"))
        ui.setup(core.parse_args(p, ["go"]))
        self.usage_error(ui.confirm_danger, "a", "b")


class Plain(UiBase):
    """Interactive without gum: plain lines on stdin."""

    def test_ask_confirm_choose_password(self):
        self.typing("typed\n\nn\n\nyes\n2\n\n")
        self.assertEqual(ui.ask("A", "A?", "d"), "typed")
        self.assertEqual(ui.ask("A", "A?", "d"), "d")  # empty line: default
        self.assertIs(ui.confirm("ok?", True), False)
        self.assertIs(ui.confirm("ok?", True), True)  # empty: default
        self.assertIs(ui.confirm("ok?"), True)
        self.assertEqual(ui.choose("C", "?", ["a", "b"]), "b")
        self.assertEqual(ui.choose("C", "?", ["a", "b"], "b"), "b")
        with mock.patch.object(ui.getpass, "getpass", return_value="pw-secret"):
            self.assertEqual(ui.password("P", "pw?"), "pw-secret")
        self.assertEqual(core.redact("pw-secret"), "***")

    def test_choose_asks_again_after_a_bad_pick(self):
        self.typing("zzz\n1\n")
        self.assertEqual(ui.choose("C", "?", ["a", "b"]), "a")

    def test_end_of_input_is_a_cancel_not_a_no(self):
        self.typing("")
        with self.assertRaises(core.Cancelled):
            ui.confirm("ok?")

    def test_confirm_danger_needs_the_target_typed_exactly(self):
        self.typing("host1\nyes\n")
        self.assertTrue(ui.confirm_danger("x", "host1", "this locks you out"))
        self.assertFalse(ui.confirm_danger("x", "host1"))  # "yes" is not the target


class Gum(UiBase):
    def stub(self, version="0.14.3", rc=""):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        wr(bin_dir / "gum", GUM_STUB)
        os.chmod(bin_dir / "gum", 0o755)
        os.environ.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", GUM_VERSION=version, GUM_RC=rc)
        self.enter(mock.patch.object(ui, "interactive", return_value=True))
        self.enter(redirect_stderr(io.StringIO()))

    def test_gum_answers_and_cancels(self):
        self.stub()
        self.assertEqual(ui.ask("A", "A?", "d"), "from-gum")
        self.assertEqual(ui.choose("C", "?", ["x", "y"], "y"), "x")  # the stub echoes the first option
        self.assertEqual(ui.password("P", "pw?"), "from-gum")
        self.assertTrue(ui.confirm("ok?"))
        os.environ["GUM_RC"] = "1"
        self.assertIs(ui.confirm("ok?"), False)
        with self.assertRaises(core.Cancelled):
            ui.ask("A", "A?")  # Esc on an input is a cancel
        os.environ["GUM_RC"] = "130"
        with self.assertRaises(core.Cancelled):
            ui.confirm("ok?")
        os.environ["GUM_RC"] = "7"
        with self.assertRaises(core.Fail):
            ui.ask("A", "A?")

    def test_a_too_old_gum_falls_back_to_plain_prompts(self):
        self.stub(version="0.1.0")
        with mock.patch.object(sys, "stdin", io.StringIO("plain\n")):
            self.assertEqual(ui.ask("A", "A?"), "plain")


class Flow(UiBase):
    FINDINGS = [
        ui.Finding("a", "warn", "Raise limit", "10", "20", "fewer 502s", "more memory"),
        ui.Finding("b", "info", "Rename pool", "www", "app", "clarity", "none"),
        ui.Finding("c-1", "crit", "Set size", "8", "16", "fits RAM", "swap if wrong", check=lambda v: None if v.isdigit() else "must be a number"),
    ]

    def run_review(self):
        err = io.StringIO()
        with redirect_stderr(err):
            got = ui.review(self.FINDINGS)
        return got, err.getvalue()

    def test_review_applies_skips_and_edits_per_answers(self):
        self.answer("FINDING_A", "apply")
        self.answer("FINDING_B", "skip")
        self.answer("FINDING_C_1", "edit")
        self.answer("VALUE_C_1", "12")
        got, shown = self.run_review()
        self.assertEqual([(f.id, f.proposed) for f in got], [("a", "20"), ("c-1", "12")])
        self.assertEqual(self.FINDINGS[2].proposed, "16")  # the input list is not mutated
        for text in ("Raise limit", "fewer 502s", "more memory", "10", "20"):
            self.assertIn(text, shown)

    def test_review_rejects_a_bad_edit_when_nobody_can_correct_it(self):
        self.answer("FINDING_A", "skip")
        self.answer("FINDING_B", "skip")
        self.answer("FINDING_C_1", "edit")
        self.answer("VALUE_C_1", "lots")
        with redirect_stderr(io.StringIO()):
            msg = self.usage_error(ui.review, self.FINDINGS)
        self.assertIn("c-1: must be a number", msg)

    def test_review_with_accept_defaults_applies_every_recommendation(self):
        self.accept_defaults()
        got, _ = self.run_review()
        self.assertEqual([f.id for f in got], ["a", "b", "c-1"])
        ui.setup()
        with redirect_stderr(io.StringIO()):
            self.assertIn("--finding-a", self.usage_error(ui.review, self.FINDINGS))

    def test_preview_prints_a_unified_diff_per_changed_file(self):
        old, same = self.tmp / "old.conf", self.tmp / "same.conf"
        wr(old, "a=1\nb=2\n")
        wr(same, "x\n")
        edits = [types.SimpleNamespace(path=str(old), content="a=1\nb=3\n"), types.SimpleNamespace(path=str(self.tmp / "new.conf"), content="n=1\n"),
                 types.SimpleNamespace(path=str(same), content="x\n")]
        out = io.StringIO()
        self.assertEqual(ui.preview(edits, file=out), 2)
        text = out.getvalue()
        self.assertIn(f"--- {old}", text)
        self.assertIn(f"+++ {old}", text)
        self.assertIn("-b=2", text)
        self.assertIn("+b=3", text)
        self.assertIn("--- /dev/null", text)
        self.assertIn("+n=1", text)
        self.assertIn(f"unchanged: {same}", text)


class Output(UiBase):
    def test_table_aligns_columns_with_a_header_rule(self):
        out = io.StringIO()
        ui.table([("a", 1), ("longer", 22)], headers=("name", "n"), file=out)
        self.assertEqual(out.getvalue().splitlines(), ["name    n", "------  --", "a       1", "longer  22"])
        ui.table([], file=out)  # nothing to show, nothing printed

    def test_messages_go_to_stderr_colour_only_on_a_tty_and_never_with_no_color(self):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()) as out:
            ui.warn("careful")
        self.assertEqual((err.getvalue(), out.getvalue()), ("[WARN] careful\n", ""))
        err = io.StringIO()
        err.isatty = lambda: True
        with redirect_stderr(err):
            ui.warn("careful")
            os.environ["NO_COLOR"] = "1"
            ui.error("bad")
        lines = err.getvalue().splitlines()
        self.assertEqual(lines[0], "\x1b[33m[WARN]\x1b[0m careful")
        self.assertEqual(lines[1], "[ERROR] bad")

    def test_registered_secrets_never_reach_the_screen(self):
        core.register_secret("hunter22")
        err = io.StringIO()
        with redirect_stderr(err):
            ui.info("login hunter22 failed")
            ui.section("hunter22")
        self.assertNotIn("hunter22", err.getvalue())


if __name__ == "__main__":
    unittest.main()
