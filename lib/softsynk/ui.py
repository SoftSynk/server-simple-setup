"""Prompts and output for the softsynk Python tools, with the rules of lib/ui.sh.

A prompt is answered by a pre-supplied answer (env SOFTSYNK_ANSWER_<NAME>, taken once), then by gum
when interactive (plain terminal lines when gum is missing or too old). Non-interactive means: no TTY on
stdin and stderr, --non-interactive, SOFTSYNK_NONINTERACTIVE=1 or SOFTSYNK_INTERACTIVE=0. Then prompts
never wait: they use their default only with --accept-defaults, and otherwise raise Fail(..., 64) naming
the option. Cancelling (Ctrl-C, Esc, end of input) raises Cancelled (130), which is never "No".
confirm_danger is never satisfied by defaults: it needs the target typed, or --confirm ACTION=TARGET.

Dialogue and messages go to stderr; table() and preview() are reports and go to stdout.
"""
import argparse
import dataclasses
import difflib
import getpass
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from . import core

MIN_GUM = (0, 13)
_state = {"non_interactive": False, "accept_defaults": False, "confirmed": frozenset()}


def setup(args=None):
    """Take --non-interactive, --accept-defaults and --confirm from parsed args (None resets to defaults)."""
    _state["non_interactive"] = bool(getattr(args, "non_interactive", False))
    _state["accept_defaults"] = bool(getattr(args, "accept_defaults", False))
    _state["confirmed"] = frozenset(getattr(args, "confirm", None) or ())


def confirm_arg(parser):
    """--confirm ACTION=TARGET (repeatable): the scripted form of confirm_danger. Add it to tools that have one.
    On a parser or on a subparser, not both: argparse lets the subcommand's values replace the parent's."""
    parser.add_argument("--confirm", action="append", default=argparse.SUPPRESS, metavar="ACTION=TARGET",
                        help="pre-confirm one destructive step, naming its target")


def interactive():
    e = os.environ
    return (e.get("SOFTSYNK_INTERACTIVE") != "0" and e.get("SOFTSYNK_NONINTERACTIVE") != "1" and not _state["non_interactive"]
            and sys.stdin.isatty() and sys.stderr.isatty())


def _accept_defaults():
    return _state["accept_defaults"] or os.environ.get("SOFTSYNK_ACCEPT_DEFAULTS") == "1"


def _opt(name):
    return "--" + name.lower().replace("_", "-")


def _need(msg):
    raise core.Fail("Non-interactive: " + msg, core.USAGE)


def _take(name):
    """The pre-supplied answer, removed from the environment so a retry cannot reuse it and children never see it."""
    return os.environ.pop("SOFTSYNK_ANSWER_" + name, None)


# ---------------------------------------------------------------- gum and plain input

def _gum_ok():
    path = shutil.which("gum")
    if not path:
        return False
    m = re.search(r"(\d+)\.(\d+)", core.run([path, "--version"], timeout=5).out)
    return bool(m) and (int(m[1]), int(m[2])) >= MIN_GUM


def _gum(*args, capture=True):
    """(exit code, stdout). gum draws on the terminal; its stdout is the answer."""
    r = subprocess.run(["gum", *args], stdout=subprocess.PIPE if capture else None, text=True)
    return r.returncode, (r.stdout or "").rstrip("\n")


def _gum_answer(*args):
    rc, out = _gum(*args)
    if rc in (1, 130):
        raise core.Cancelled()
    if rc:
        raise core.Fail(f"gum failed (exit {rc}).")
    return out


def _line(prompt, secret=False):
    try:
        if secret:
            return getpass.getpass(prompt, stream=sys.stderr)
        sys.stderr.write(prompt)
        sys.stderr.flush()
        s = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        s = ""
    if s == "":  # end of input or Ctrl-C
        print(file=sys.stderr)
        raise core.Cancelled()
    return s.rstrip("\n")


# ---------------------------------------------------------------- prompts

def ask(name, question, default=None):
    """Free text. Empty input gives the default ("" without one)."""
    v = _take(name)
    if v is None:
        if interactive():
            if _gum_ok():
                v = _gum_answer("input", "--header", question, "--value", default or "")
            else:
                v = _line(f"{question}" + (f" [{default}]" if default else "") + ": ")
        elif default is not None and _accept_defaults():
            v = default
        elif default is not None:
            _need(f'"{question}" has a default; pass {_opt(name)} VALUE (or SOFTSYNK_ANSWER_{name}=VALUE) or --accept-defaults.')
        else:
            _need(f'"{question}" has no default; pass {_opt(name)} VALUE (or SOFTSYNK_ANSWER_{name}=VALUE).')
    return v or default or ""


def confirm(question, default=False):
    """Yes/no. Not answerable by environment, only by a person or --accept-defaults."""
    if not interactive():
        if _accept_defaults():
            return default
        _need(f'"{question}" needs an answer; pass --accept-defaults to take the default ({"Y" if default else "N"}).')
    if _gum_ok():
        rc, _ = _gum("confirm", f"--default={'true' if default else 'false'}", question, capture=False)
        if rc in (0, 1):
            return rc == 0
        if rc == 130:
            raise core.Cancelled()
        raise core.Fail(f"gum failed (exit {rc}).")
    while True:
        s = _line(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not s:
            return default
        if s in ("y", "yes", "n", "no"):
            return s[0] == "y"


def _match(v, options):
    """An option picked by 1-based number, full text or first word, ignoring case; None when nothing fits."""
    v = v.strip().lower()
    if v.isdigit() and 1 <= int(v) <= len(options):
        return options[int(v) - 1]
    return next((o for o in options if v in (o.lower(), o.split()[0].lower())), None)


def choose(name, title, options, default=None):
    """One of `options` (the text is returned). `default` is an option; the first when omitted."""
    options = list(options)
    default = options[0] if default is None else default
    if default not in options:
        raise ValueError(f"choose: default {default!r} is not one of the options for {title!r}")
    v = _take(name)
    if v is not None:
        pick = _match(v, options)
        if pick is None:
            raise core.Fail(f"Invalid value for {_opt(name)} (\"{title}\"): use 1-{len(options)} or the option's first word.", core.USAGE)
        return pick
    if not interactive():
        if _accept_defaults():
            return default
        _need(f'"{title}" needs an answer; pass {_opt(name)} VALUE (or SOFTSYNK_ANSWER_{name}=VALUE) or --accept-defaults.')
    if _gum_ok():
        return _gum_answer("choose", "--header", title, "--selected", default, *options)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}", file=sys.stderr)
    while True:
        s = _line(f"{title} [{options.index(default) + 1}]: ")
        pick = default if not s.strip() else _match(s, options)
        if pick is not None:
            return pick
        error(f"Choose a number from 1 to {len(options)}.")


def password(name, question):
    """Hidden input. Non-interactively only SOFTSYNK_ANSWER_<NAME> works; never a default, never an option.
    The value is registered for redaction before it is returned."""
    v = _take(name)
    if v is None:
        if not interactive():
            _need(f'"{question}" is a secret; set SOFTSYNK_ANSWER_{name} in the environment (never as an option).')
        v = _gum_answer("input", "--password", "--header", question) if _gum_ok() else _line(question + ": ", secret=True)
    core.register_secret(v)
    return v


def confirm_danger(action, target, why=""):
    """For destructive steps. True = confirmed, False = refused (anything but the exact target typed)."""
    if f"{action}={target}" in _state["confirmed"]:
        return True
    if not interactive():
        _need(f"destructive step '{action}' on '{target}' needs --confirm {action}={target}.")
    if why:
        warn(why)
    header = f"Type {target} to confirm (anything else cancels this step)"
    typed = _gum_answer("input", "--header", header, "--placeholder", target) if _gum_ok() else _line(header + ": ")
    if typed == target:
        return True
    warn("Not confirmed; this step was skipped.")
    return False


# ---------------------------------------------------------------- output

def _c(code, text):
    return f"\x1b[{code}m{text}\x1b[0m" if sys.stderr.isatty() and "NO_COLOR" not in os.environ else text


def _say(level, code, msg):
    print(f"{_c(code, '[' + level + ']')} {core.redact(msg)}", file=sys.stderr)


def info(msg):
    _say("INFO", "34", msg)


def ok(msg):
    _say("OK", "32", msg)


def warn(msg):
    _say("WARN", "33", msg)


def error(msg):
    _say("ERROR", "31", msg)


def section(title):
    print(f"\n{_c('34', '== ' + core.redact(title) + ' ==')}", file=sys.stderr)


def say(msg):
    print("    " + core.redact(msg), file=sys.stderr)


def table(rows, headers=(), file=None):
    """Left-aligned columns, an optional header with a rule under it. Prints to stdout."""
    rows = [[str(c) for c in r] for r in rows]
    if headers:
        rows.insert(0, [str(h) for h in headers])
    if not rows:
        return
    w = [max(len(r[i]) for r in rows if i < len(r)) for i in range(max(map(len, rows)))]
    for n, r in enumerate(rows):
        print("  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip(), file=file or sys.stdout)
        if headers and n == 0:
            print("  ".join("-" * x for x in w), file=file or sys.stdout)


# ---------------------------------------------------------------- the unified flow: explain, customize, preview

@dataclass
class Finding:
    """One recommendation. `severity` ("info", "warn", "crit") only changes how it is shown."""
    id: str
    severity: str
    title: str
    current: str
    proposed: str
    benefit: str
    risk: str
    check: Optional[Callable[[str], Optional[str]]] = None  # check(new_value) -> problem text, or None when acceptable


def _env_name(s):
    return re.sub(r"\W", "_", s.upper())


def _edit(f):
    name = "VALUE_" + _env_name(f.id)
    while True:
        v = ask(name, f"New value for {f.title}", str(f.proposed))
        problem = f.check(v) if f.check else None
        if not problem:
            return v
        if not interactive():  # nobody to correct it, and the answer was consumed
            raise core.Fail(f"{f.id}: {problem}", core.USAGE)
        error(problem)


def review(findings):
    """Explain each finding, then ask apply, skip or edit. Returns the findings to apply (an edited one is a
    copy whose proposed value is the new text). Scripted: FINDING_<ID> and VALUE_<ID> answers; --accept-defaults applies all."""
    out = []
    for f in findings:
        section(f"{f.title} [{f.severity}]")
        for label, text in (("Current", f.current), ("Proposed", f.proposed), ("Benefit", f.benefit), ("Risk", f.risk)):
            say(f"{label + ':':<10}{text}")
        a = choose("FINDING_" + _env_name(f.id), "What should happen?", ["apply", "skip", "edit"], "apply")
        if a != "skip":
            out.append(dataclasses.replace(f, proposed=_edit(f)) if a == "edit" else f)
    return out


def _text(x):
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else x


def preview(edits, file=None):
    """Unified diff of what change.apply() would write, one block per file (anything with .path and .content).
    Returns how many files differ. Prints to stdout."""
    out, n = file or sys.stdout, 0
    for e in edits:
        try:
            with open(e.path, encoding="utf-8", errors="replace") as f:
                old = f.read()
        except OSError:
            old = None
        new = _text(e.content)
        if old == new:
            print(f"unchanged: {e.path}", file=out)
            continue
        n += 1
        for line in difflib.unified_diff((old or "").splitlines(), new.splitlines(), e.path if old is not None else "/dev/null", e.path, lineterm=""):
            print(line, file=out)
    return n
