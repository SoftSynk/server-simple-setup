"""Foundation shared by every softsynk Python tool: root-tree paths, validated config, atomic files,
child processes, locks, logs with secret redaction, status documents and the CLI wrapper.

Conventions are those of synk-backup and softsynk-server (exit codes, root layout, status schema 1).
"""
import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import uuid

OK, DEGRADED, FAILED, INCOMPLETE, USAGE, CANCELLED = 0, 1, 2, 3, 64, 130
DEFAULT_ROOT = "/opt/softsynk-server-management"
SCHEMA = 1
LOG_KEEP_DAYS = 90
KINDS = ("etc", "secrets", "state", "log", "data", "tmp")
INSTALL_STATES = ("installed", "adopted", "not_installed", "unknown")
HEALTHS = ("healthy", "degraded", "unhealthy", "unknown", "stale", "unsupported")
HEALTH_EXIT = {"healthy": 0, "degraded": 1, "unhealthy": 2, "unknown": 3, "stale": 3}  # --status exit code by health
ROOT_RE = re.compile(r"^/[A-Za-z0-9_./-]*$")  # root and tool path end up in a unit file: keep them plain
BIN_RE = re.compile(r"^/[A-Za-z0-9_./+-]*$")


class Fail(Exception):
    """An expected failure: message for the operator, exit code for the shell."""
    def __init__(self, message, code=FAILED):
        super().__init__(message)
        self.code = code


class Corrupt(Fail):
    """A state file exists but cannot be parsed; callers may recover, otherwise it is a failure."""
    def __init__(self, path, reason):
        super().__init__(f"corrupt or unreadable {path}: {reason}")
        self.path = path


class Busy(Fail):
    """Another run holds this module's lock."""


class Cancelled(Fail):
    """The operator cancelled (Ctrl-C, Esc, end of input). Never means "No"."""
    def __init__(self, message="cancelled"):
        super().__init__(message, CANCELLED)


# ---------------------------------------------------------------- time

def now():
    """Local time, timezone-aware. SOFTSYNK_NOW (ISO) is a test-only clock."""
    v = os.environ.get("SOFTSYNK_NOW")
    t = dt.datetime.fromisoformat(v) if v else dt.datetime.now()
    return (t.astimezone() if t.tzinfo is None else t).replace(microsecond=0)


def iso(t):
    return t.isoformat(timespec="seconds")


def parse_iso(s):
    """Aware datetime (naive input is taken as local time), or None when s is not an ISO timestamp."""
    try:
        t = dt.datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None
    return t.astimezone() if t is not None and t.tzinfo is None else t


def human(n):
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


# ---------------------------------------------------------------- paths

class Ctx:
    """Where one module keeps its files: <root>/{etc,secrets,state,log,data,tmp}/<module>, lock/<module>.lock."""

    def __init__(self, module, root=None):
        self.module = module
        self.root = os.path.abspath(root or os.environ.get("SOFTSYNK_ROOT") or DEFAULT_ROOT)
        for k in KINDS:
            setattr(self, k, os.path.join(self.root, k, module))
        self.config_path = os.path.join(self.etc, "config.json")
        self.lock_path = os.path.join(self.root, "lock", module + ".lock")
        self.run_id = uuid.uuid4().hex[:8]
        self._pruned = False

    def path(self, kind, *parts):
        if kind not in KINDS:
            raise ValueError(f"unknown path kind {kind!r}")
        return os.path.join(getattr(self, kind), *parts)

    def other(self, module):
        """The same root seen as another module (e.g. notify's config)."""
        return Ctx(module, self.root)


# ---------------------------------------------------------------- files

def ensure_dir(path, mode=0o755):
    os.makedirs(path, mode=mode, exist_ok=True)


def write_atomic(path, data, mode, owner=None):
    """tmp file in the same directory + fsync + rename + directory fsync: readers see the old or the new file,
    never half, and the new name survives a crash. owner=(uid, gid) is set before the rename (restoring a
    root-owned config as root); chown comes before chmod because chown can clear setuid/setgid bits."""
    ensure_dir(os.path.dirname(path))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            if owner:
                os.fchown(f.fileno(), *owner)
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    dfd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(dfd)
    except OSError:  # the data is in place; a filesystem that cannot sync a directory must not fail the write
        pass
    finally:
        os.close(dfd)


def write_if_changed(path, data, mode):
    """Atomic write that touches nothing when content and mode already match. True when written."""
    if isinstance(data, str):
        data = data.encode()
    try:
        with open(path, "rb") as f:
            if f.read() == data and stat.S_IMODE(os.fstat(f.fileno()).st_mode) == mode:
                return False
    except FileNotFoundError:
        pass
    write_atomic(path, data, mode)
    return True


def read_json(path, default=None):
    """Parsed JSON; `default` when the file is absent; Corrupt when it exists but is unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        raise Corrupt(path, e)


def write_json(path, obj, mode=0o644):
    write_atomic(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode(), mode)


ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_env_file(path):
    """KEY=value lines (no quoting, no comments). Missing or unreadable file -> {}."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                k, sep, v = line.rstrip("\r\n").partition("=")
                if sep and k:
                    out[k] = v
    except OSError:
        pass
    return out


def write_env_file(path, mapping):
    """Private (0600, parent 0700) KEY=value file. True when written."""
    for k, v in mapping.items():
        if not ENV_KEY_RE.match(k) or "\n" in str(v) or "\r" in str(v):
            raise Fail(f"cannot store {k!r} in an env file (bad name or multi-line value)", USAGE)
    parent = os.path.dirname(path)
    ensure_dir(parent, 0o700)
    os.chmod(parent, 0o700)
    return write_if_changed(path, "".join(f"{k}={v}\n" for k, v in mapping.items()), 0o600)


# ---------------------------------------------------------------- config

def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _type_problems(defaults, raw, prefix, problems):
    for k, v in raw.items():
        name = prefix + k
        if k not in defaults:
            problems.append(f"unknown key '{name}'")
            continue
        d = defaults[k]
        if isinstance(d, dict):
            if isinstance(v, dict):
                _type_problems(d, v, name + ".", problems)
            else:
                problems.append(f"{name}: expected object, got {type(v).__name__}")
        elif d is not None and not (type(v) in (int, float) if type(d) is float else type(v) is type(d)):
            problems.append(f"{name}: expected {type(d).__name__}, got {type(v).__name__}")


def load_config(path, defaults, check=None):
    """Validated config: the file deep-merged over `defaults`, or None when the file does not exist.

    Unknown keys and wrong types (int is fine for a float default, bool is not an int) are errors, then
    check(cfg) -> [problem, ...] runs for value rules. Any problem raises Fail(..., USAGE); never writes.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Fail(f"cannot read {path}: {e}", USAGE)
    if not isinstance(raw, dict):
        raise Fail(f"{path} must contain a JSON object", USAGE)
    problems = []
    _type_problems(defaults, raw, "", problems)
    cfg = _merge(defaults, raw)
    if not problems and check:
        problems = list(check(cfg) or [])
    if problems:
        raise Fail(f"invalid config {path}: " + "; ".join(problems), USAGE)
    return cfg


# ---------------------------------------------------------------- child processes

class Result:
    def __init__(self, rc, out="", err="", timed_out=False):
        self.rc, self.out, self.err, self.timed_out = rc, out, err, timed_out

    @property
    def ok(self):
        return self.rc == 0 and not self.timed_out

    def why(self):
        if self.timed_out:
            return "timed out"
        e = self.err.strip()
        return (e if len(e) <= 1500 else "..." + e[-1500:]) or f"exit {self.rc}"


def _kill_group(p):
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(p.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        p.wait(timeout=5)


def run(argv, timeout=None, input=None, env=None):
    """Run argv (never a shell) in its own process group; the group is killed on timeout or interrupt, never after a
    normal exit. Never raises:
    a binary that cannot start gives rc 127 and "<argv0>: <reason>" in err. Output goes through temp
    files so a background child holding a pipe cannot hang us."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as stdin:
        if input is not None:
            stdin.write(input.encode())
            stdin.flush()
            stdin.seek(0)
        try:
            p = subprocess.Popen(argv, stdin=stdin if input is not None else subprocess.DEVNULL,
                                 stdout=out, stderr=err, env=env, cwd="/", start_new_session=True)
        except OSError as e:
            return Result(127, "", f"{argv[0]}: {e.strerror}")
        timed_out = False
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(p)  # also reaps; stragglers in the group die with it
        except BaseException:  # Ctrl-C and friends: do not leave the command running
            _kill_group(p)
            raise
        # a normal exit kills nothing: a command may leave a daemon behind on purpose (systemctl start, ssh -f)
        out.seek(0)
        err.seek(0)
        return Result(p.returncode, out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace"), timed_out)


# ---------------------------------------------------------------- lock

@contextlib.contextmanager
def lock(ctx):
    """Non-blocking exclusive flock on lock/<module>.lock for the duration of the with block."""
    ensure_dir(os.path.dirname(ctx.lock_path))
    fd = os.open(ctx.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Busy(f"another softsynk {ctx.module} operation is running (lock held)")
    try:
        yield
    finally:
        os.close(fd)  # closing releases the lock


# ---------------------------------------------------------------- secrets and logs

_SECRETS = set()


def register_secret(value):
    """Never show this value in logs or output again (values shorter than 4 characters are ignored)."""
    v = "" if value is None else str(value)
    if len(v) >= 4:
        _SECRETS.add(v)
        _SECRETS.add(json.dumps(v)[1:-1])  # the form it takes inside a JSON string


def redact(s):
    s = str(s)
    for v in sorted(_SECRETS, key=len, reverse=True):
        s = s.replace(v, "***")
    return s


MONTH_FILE_RE = re.compile(r"^(\d{4})-(\d{2})\.jsonl$")


def prune_logs(ctx, keep_days=LOG_KEEP_DAYS, t=None):
    """Delete monthly log files whose whole month is older than keep_days. Only regular files named
    YYYY-MM.jsonl in the module's log dir; symlinks and other names are never touched."""
    cutoff = ((t or now()) - dt.timedelta(days=keep_days)).date()
    try:
        names = os.listdir(ctx.log)
    except OSError:
        return
    for n in names:
        m = MONTH_FILE_RE.match(n)
        full = os.path.join(ctx.log, n)
        if not m or os.path.islink(full) or not os.path.isfile(full):
            continue
        try:
            y, mo = int(m.group(1)), int(m.group(2))
            month_end = dt.date(y + mo // 12, mo % 12 + 1, 1)
        except ValueError:
            continue
        if month_end <= cutoff:
            with contextlib.suppress(OSError):
                os.unlink(full)


def log(ctx, event, **kw):
    """Append one redacted JSON line to log/<module>/YYYY-MM.jsonl (0640); prunes old months once per run.
    Best effort: a full or broken disk must not take the tool down."""
    t = now()
    try:
        if not ctx._pruned:
            ctx._pruned = True
            prune_logs(ctx, t=t)
        ensure_dir(ctx.log)
        line = redact(json.dumps(dict(kw, ts=iso(t), run=ctx.run_id, event=event), sort_keys=True, default=str)) + "\n"
        fd = os.open(os.path.join(ctx.log, t.strftime("%Y-%m") + ".jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------- status

def status_doc(module, version, install_state, health, summary="", details=None, warnings=None, actions=None):
    """The status JSON object of the module contract (schema 1); the caller prints it."""
    if install_state not in INSTALL_STATES or health not in HEALTHS:
        raise ValueError(f"bad status vocabulary: {install_state!r} / {health!r}")
    return {"schema": SCHEMA, "module": module, "version": version,
            "observed_at": now().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "install_state": install_state, "health": health, "summary": summary,
            "details": details or {}, "warnings": list(warnings or []), "actions": list(actions or [])}


def show_status(doc, as_json, json_exit_zero=True):
    """Print a status document (JSON, or a line plus its warnings) and return the exit code: 0 for JSON when
    json_exit_zero (the module contract), else by health; 3 unless the module is installed."""
    code = HEALTH_EXIT.get(doc["health"], 3) if doc["install_state"] == "installed" else 3
    if as_json:
        print(json.dumps(doc, indent=2, sort_keys=True))
        return OK if json_exit_zero else code
    print(f"{doc['module']}: {doc['install_state']}, {doc['health']}: {doc['summary']}")
    for w in doc["warnings"]:
        print(f"  ! {w}")
    return code


# ---------------------------------------------------------------- systemd units a tool writes

def unit_dir():
    return os.environ.get("SOFTSYNK_SYSTEMD_DIR") or "/etc/systemd/system"


def exec_path(ctx, tool_file):
    """What a unit should run: the stable <root>/current copy when it is this very tool, else tool_file itself."""
    here = os.path.realpath(tool_file)
    cur = os.path.join(ctx.root, "current", "deployment", ctx.module, os.path.basename(here))
    return cur if os.path.exists(cur) and os.path.realpath(cur) == here else here


# ---------------------------------------------------------------- CLI

class Parser(argparse.ArgumentParser):
    """argparse that exits 64 (bad request) instead of 2 on usage errors."""
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        sys.exit(USAGE)


COMMON = {"root": None, "non_interactive": False, "accept_defaults": False, "dry_run": False, "json": False}


def common_args(parser, json=True):
    """Options every tool shares. Defaults are suppressed so the same options may sit on the parser and
    on its subcommands; parse_args() fills the real defaults in. json=False: this command has no JSON output."""
    s = argparse.SUPPRESS
    parser.add_argument("--root", default=s, help=f"management root (default: $SOFTSYNK_ROOT or {DEFAULT_ROOT})")
    parser.add_argument("--non-interactive", action="store_true", default=s, help="never prompt (also: SOFTSYNK_NONINTERACTIVE=1)")
    parser.add_argument("--accept-defaults", action="store_true", default=s, help="non-interactive: take each prompt's default")
    parser.add_argument("--dry-run", action="store_true", default=s, help="show what would change; change nothing")
    if json:
        parser.add_argument("--json", action="store_true", default=s, help="machine-readable output")


def parse_args(parser, argv=None):
    a = parser.parse_args(argv)
    for k, v in COMMON.items():
        if not hasattr(a, k):
            setattr(a, k, v)
    # The engine passes `softsynk-server install --dry-run` to modules only through the environment.
    if os.environ.get("SOFTSYNK_DRY_RUN") == "1":
        a.dry_run = True
    return a


def main(fn, prog=None):
    """Run fn() and return its exit code (None -> 0). Fail -> its code, Ctrl-C -> 130. Use: sys.exit(main(run))."""
    prog = prog or os.path.basename(sys.argv[0])
    try:
        return fn() or OK
    except Cancelled:
        print(f"{prog}: cancelled", file=sys.stderr)
        return CANCELLED
    except Fail as f:
        print(f"{prog}: {redact(f)}", file=sys.stderr)
        return f.code
    except KeyboardInterrupt:
        print(f"{prog}: cancelled", file=sys.stderr)
        return CANCELLED
    except BrokenPipeError:
        return OK
