"""The Apply / Verify / Rollback step of the unified flow, for config files owned by the system.

apply() writes edits so that a bad config never stays live:
  check the CURRENT config (refuse when it is already broken), back up every file, write atomically
  (owner and mode kept), validate, reload, verify. Any failure puts every file back byte for byte and
  reloads the old config again (restart when the reload will not take); the error says what happened.
Backups live in data/<module>/backups/<stamp>/ (dir 0700): copies named 0, 1, ... plus manifest.json with
path, existed, mode, uid, gid and sha256, so restore() can undo a change later, even in another process.

validate, reload and restart are one argv or a list of argvs. The caller holds core.lock(ctx);
nothing here locks.

Assumptions (documented, not enforced):
- Trust: edit paths are resolved with realpath and the target is written, so a symlink in the path is
  followed. That is only safe when the parent directories are root-owned and not writable by anyone
  else (/etc/ssh, /etc/php, /etc/systemd ...); never point an Edit at a directory a lower-privileged user controls.
- reload and restart are systemctl argvs ("systemctl", "reload", "ssh.service"): idempotent, safe to run
  again, and the same on the way back. A rollback runs them a second time, with the old files in place.
  restart is only the fallback of reload, so restart without reload is refused.
- latest() and restore(stamp=None) order backups by stamp, which is wall-clock time. A clock stepped
  backwards makes an older backup look newer; pass the stamp explicitly when that matters.
"""
import contextlib
import hashlib
import os
import re
import shutil
import signal
import stat
import sys
from dataclasses import dataclass
from typing import Optional, Union

from . import core

STAMP = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


@dataclass
class Edit:
    """Wanted content for one file. mode=None keeps the file's mode (0644 for a new file)."""
    path: str
    content: Union[str, bytes]
    mode: Optional[int] = None


def _argvs(x):
    x = list(x or ())
    return x if not x or isinstance(x[0], (list, tuple)) else [x]


def _check(run, argvs, timeout, what):
    """None when every command succeeds, else 'what: command: first line of its error' for the first that
    fails. One line only: a tool's stderr can echo the config it choked on, secrets included."""
    for argv in argvs:
        r = run(list(argv), timeout=timeout)
        if not r.ok:
            return f"{what}: {' '.join(argv)}: {(r.why().splitlines() or [''])[0][:200]}"
    return None


def _need_reload(reload, restart):
    if restart and not reload:
        raise core.Fail("restart is only the fallback of reload; give a reload command too.")


_STOP = (signal.SIGHUP, signal.SIGTERM)


def _terminate(signum, frame):
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def _signals(handler, sigs=_STOP):
    """Run the block with `handler` on `sigs`, then put the old handlers back. Only the main thread may set
    handlers; anywhere else this does nothing."""
    try:
        old = {s: signal.signal(s, handler) for s in sigs}
    except ValueError:
        old = {}
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _stat(path):
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None


def _sha(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- backups

def _base(ctx):
    return ctx.path("data", "backups")


def _stamps(ctx):
    """Complete backup directories (with a manifest), oldest first; -N orders backups made in the same second."""
    base = _base(ctx)
    try:
        names = os.listdir(base)
    except FileNotFoundError:
        return []
    ok = (n for n in names if STAMP.match(n) and not os.path.islink(os.path.join(base, n))
          and os.path.isfile(os.path.join(base, n, "manifest.json")))
    return sorted(ok, key=lambda s: (s[:15], int(s[16:] or 1)))


def latest(ctx):
    """Stamp of the newest backup, or None. Newest means latest wall-clock time (see the module docstring)."""
    s = _stamps(ctx)
    return s[-1] if s else None


def prune(ctx, keep=20):
    """Remove all but the newest `keep` backups. Anything else in the directory is left alone."""
    s = _stamps(ctx)
    for name in s[:max(len(s) - keep, 0)]:
        shutil.rmtree(os.path.join(_base(ctx), name), ignore_errors=True)


def backup(ctx, paths):
    """Copy `paths` (absent ones are recorded as such) into a new stamped directory; returns the stamp.
    A failure part way removes the directory again and raises Fail."""
    base = _base(ctx)
    stamp = core.now().strftime("%Y%m%d-%H%M%S")
    name, n = stamp, 1
    try:
        core.ensure_dir(base, 0o700)
        os.chmod(base, 0o700)
        while True:
            d = os.path.join(base, name)
            try:
                os.mkdir(d, 0o700)
                break
            except FileExistsError:
                n += 1
                name = f"{stamp}-{n}"
        os.chmod(d, 0o700)
    except OSError as err:
        raise core.Fail(f"Could not create a backup in {base}: {err}")
    try:
        entries = []
        for i, path in enumerate(paths):
            st, data = _stat(path), _read(path)
            e = {"path": path, "existed": data is not None, "file": None}
            if data is not None:
                core.write_atomic(os.path.join(d, str(i)), data, 0o600)
                e.update(file=str(i), mode=stat.S_IMODE(st.st_mode), uid=st.st_uid, gid=st.st_gid, sha256=_sha(data))
            entries.append(e)
        core.write_json(os.path.join(d, "manifest.json"), {"created": core.iso(core.now()), "entries": entries}, 0o600)
    except BaseException as err:
        shutil.rmtree(d, ignore_errors=True)
        if isinstance(err, Exception):
            raise core.Fail(f"Backup failed, nothing was changed: {err}")
        raise
    return name


def _restore_files(ctx, stamp):
    """Put every file of one backup back, going through all entries. Returns (restored paths, problems);
    raises Fail only when the backup itself cannot be found or read."""
    if not STAMP.match(stamp):
        raise core.Fail(f"'{stamp}' is not a backup stamp.")
    d = os.path.join(_base(ctx), stamp)
    try:
        m = core.read_json(os.path.join(d, "manifest.json"))
    except core.Corrupt as c:
        raise core.Fail(f"Backup {stamp} is unreadable: {c}")
    if not m:
        raise core.Fail(f"No backup {stamp} in {_base(ctx)}.")
    restored, problems = [], []
    for e in m["entries"]:
        try:
            if not e["existed"]:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(e["path"])
            else:
                data = _read(os.path.join(d, e["file"]))
                if data is None or _sha(data) != e["sha256"]:
                    problems.append(f"{e['path']}: backup copy fails its checksum, left untouched")
                    continue
                core.write_atomic(e["path"], data, e["mode"], owner=(e["uid"], e["gid"]))
            restored.append(e["path"])
        except (OSError, KeyError) as err:
            problems.append(f"{e.get('path')}: {err}")
    return restored, problems


def _reapply(run, reload, restart, timeout):
    """Make the service run the restored config: reload, else restart. (ok, notes). No reload command: nothing to do."""
    if not reload:
        return True, []
    p = _check(run, reload, timeout, "reload")
    if not p:
        return True, ["service reloaded with the previous config"]
    if restart:
        q = _check(run, restart, timeout, "restart")
        if not q:
            return True, [f"service restarted with the previous config ({p})"]
        return False, [f"{p}, then {q}; the service may not be running the previous config, fix it by hand"]
    return False, [f"{p}; the service may not be running the previous config, fix it by hand"]


def _undo(ctx, stamp, reapply, reload, restart, run, timeout):
    """Restore a backup's files, then (when `reapply`) reload/restart if at least one file went back.
    Returns (clean, notes); clean means every file is back and the service runs it. Never raises."""
    try:
        restored, problems = _restore_files(ctx, stamp)
    except core.Fail as f:
        return False, [str(f)]
    notes = ["every changed file was restored"] if not problems else \
        [f"restore incomplete, {len(restored)} of {len(restored) + len(problems)} files back: " + "; ".join(problems)]
    ok = not problems
    if reapply and restored:
        r_ok, more = _reapply(run, reload, restart, timeout)
        notes += more
        ok = ok and r_ok
    elif reapply:
        notes.append("the service was not reloaded because no file could be restored")
    return ok, notes


def restore(ctx, stamp=None, reload=(), restart=(), run=core.run, timeout=60):
    """Undo a change: files from backup `stamp` (default the latest), then reload/restart.
    Returns {stamp, restored, notes}; raises Fail when a file or the service could not be put right."""
    reload, restart = _argvs(reload), _argvs(restart)
    _need_reload(reload, restart)
    stamp = stamp or latest(ctx)
    if not stamp:
        raise core.Fail(f"No backups to restore in {_base(ctx)}.")
    restored, problems = _restore_files(ctx, stamp)
    ok, notes = _reapply(run, reload, restart, timeout) if restored else (True, [])
    core.log(ctx, "change.restored", stamp=stamp, files=restored, problems=problems, ok=ok)
    if problems or not ok:
        raise core.Fail(f"Restore from backup {stamp} incomplete: " + "; ".join(problems + notes) + ".")
    return {"stamp": stamp, "restored": restored, "notes": notes}


# ---------------------------------------------------------------- apply

def apply(ctx, edits, validate=(), reload=(), restart=(), verify=None, precheck=True, run=core.run, timeout=60):
    """Write `edits` safely (see the module docstring). verify() returns None or a problem text.
    precheck=False skips the check of the current config, for a validate command that names a file which does not exist yet.
    Any failure, including an exception or SIGHUP/SIGTERM (turned into SystemExit while this runs), restores the
    files and reloads the old config before it propagates; a clean rollback deletes its own backup.
    Returns {"changed": [paths], "stamp": backup stamp or None}; nothing to do touches nothing."""
    validate, reload, restart = _argvs(validate), _argvs(reload), _argvs(restart)
    _need_reload(reload, restart)
    todo = []
    for e in edits:
        path, data = os.path.realpath(e.path), e.content.encode() if isinstance(e.content, str) else e.content
        st = _stat(path)
        if _read(path) != data or (e.mode is not None and st and stat.S_IMODE(st.st_mode) != e.mode):
            todo.append((path, data, e.mode))
    changed = [t[0] for t in todo]
    if not todo:
        return {"changed": changed, "stamp": None}
    p = _check(run, validate, timeout, "check") if precheck else None
    if p:
        raise core.Fail(f"The current config already fails its check, so nothing was changed. Fix that first ({p}).")
    stamp, problem, reloaded, done, notes = None, None, False, False, []
    try:
        with _signals(_terminate):
            stamp = backup(ctx, changed)
            for path, data, mode in todo:
                st = _stat(path)
                if mode is None:
                    mode = stat.S_IMODE(st.st_mode) if st else 0o644
                core.write_atomic(path, data, mode, owner=(st.st_uid, st.st_gid) if st else None)
            problem = _check(run, validate, timeout, "validation failed")
            if not problem and reload:
                reloaded = True
                problem = _check(run, reload, timeout, "reload failed")
            if not problem and verify:
                try:
                    v = verify()
                except core.Fail as f:
                    v = str(f)
                problem = f"verification failed: {v}" if v else None
            done = not problem
    except OSError as err:
        problem = f"change failed: {err}"
    finally:
        if stamp and not done:  # a failure or an exception in flight: put everything back first
            with _signals(signal.SIG_IGN, _STOP + (signal.SIGINT,)):  # the way back must not be interrupted
                clean, notes = _undo(ctx, stamp, reloaded, reload, restart, run, timeout)
                core.log(ctx, "change.rolled_back", stamp=stamp, problem=problem or repr(sys.exc_info()[1]), notes=notes)
                if clean:  # the backup equals the live files again; keeping it would make latest() a no-op restore
                    shutil.rmtree(os.path.join(_base(ctx), stamp), ignore_errors=True)
    if done:
        core.log(ctx, "change.applied", stamp=stamp, files=changed)
        prune(ctx)
        return {"changed": changed, "stamp": stamp}
    raise core.Fail(f"{problem}. " + "; ".join(notes) + ".")
