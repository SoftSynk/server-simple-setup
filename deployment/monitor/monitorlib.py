"""monitorlib: what synk-monitor and synk-dash share. Python 3.11+ standard library plus lib/softsynk.

Sampling (/proc, statvfs, systemctl), the alert state machine (evaluate, a pure function: state in, state and
events out), mail text, daily JSONL history and the state files. Nothing here prints; the two scripts do.
"""
import copy
import json
import os
import re
import shlex
import socket
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "lib"))

from softsynk import core, notify, sysinfo  # noqa: E402

RUN_DIR = "/run/softsynk-monitor"  # where state goes when the state directory cannot be written (disk full, read-only)
INF = float("inf")
MARGIN = {"cpu": 5, "memory": 5, "swap": 5, "disk": 2, "inodes": 2}  # a level clears this many points below its threshold
RETRY = timedelta(minutes=15)  # a mail that failed is tried again after this
MIN_FREE = 64 << 20  # history pauses when its filesystem has less free space than this
UP = ("active", "reloading")
WORD = ("OK", "WARNING", "CRITICAL")
ALERTS = ("open", "escalate", "reminder")
SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@_.:-]*\.service$")
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@_.:-]*\.[a-z]+$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")

DEFAULTS = {
    "check_interval_sec": 300,  # how often the timer runs (setup writes the timer from this)
    "thresholds": {  # percent; a level needs `sustain` samples in a row; disk and inodes alarm at once
        "cpu": {"warn": 85, "crit": 95, "sustain": 3},
        "memory": {"warn": 85, "crit": 95, "sustain": 2},
        "swap": {"warn": 80, "crit": 95, "sustain": 2},
        "disk": {"warn": 80, "crit": 90, "sustain": 1},
        "inodes": {"warn": 80, "crit": 90, "sustain": 1},
        "services": {"sustain": 1},
        "failed_units": {"sustain": 2},
    },
    "services": [],  # units that must run, e.g. "nginx.service"
    "ignore_units": [],  # failed units that are not counted
    "exclude_mounts": [],  # mount points that are not watched
    "recover_checks": 2,  # calm samples in a row before a level clears
    "notify_interval_hours": 6,  # reminder while trouble lasts
    "summary_every_days": 7,  # 0 switches the summary mail off
    "history": {"healthy_every_min": 60, "unhealthy_every_min": 5, "retention_days": 7, "max_mb": 1024},
}


def check(cfg):
    """Value rules for load_config (types are checked there): a list of problems."""
    p, t, h = [], cfg["thresholds"], cfg["history"]
    for k in ("cpu", "memory", "swap", "disk", "inodes"):
        if not 1 <= t[k]["warn"] < t[k]["crit"] <= 100:
            p.append(f"thresholds.{k}: need 1 <= warn < crit <= 100 (percent)")
    p += [f"thresholds.{k}.sustain must be at least 1" for k, v in t.items() if v["sustain"] < 1]
    if not 30 <= cfg["check_interval_sec"] <= 3600:
        p.append("check_interval_sec must be between 30 and 3600")
    if cfg["recover_checks"] < 1:
        p.append("recover_checks must be at least 1")
    if not 1 <= cfg["notify_interval_hours"] <= 168:
        p.append("notify_interval_hours must be between 1 and 168")
    if not 0 <= cfg["summary_every_days"] <= 365:
        p.append("summary_every_days must be between 0 (off) and 365")
    p += [f"services: {s!r} is not a unit name like nginx.service" for s in cfg["services"] if not SERVICE_RE.match(s)]
    p += [f"ignore_units: {s!r} is not a unit name" for s in cfg["ignore_units"] if not UNIT_RE.match(s)]
    p += [f"exclude_mounts: {s!r} is not an absolute path" for s in cfg["exclude_mounts"] if not s.startswith("/")]
    p += [f"history.{k} must be at least 1" for k, v in h.items() if v < 1]
    return p


def load(ctx):
    """The validated config (defaults when there is no file). Raises core.Fail (USAGE) for an invalid one."""
    return core.load_config(ctx.config_path, DEFAULTS, check) or copy.deepcopy(DEFAULTS)


# ---------------------------------------------------------------- sampling

@dataclass
class Env:
    """Everything that touches the host, so tests can swap it."""
    proc: str = "/proc"
    run: Callable = core.run
    statvfs: Callable = os.statvfs
    send: Callable = notify.send
    run_dir: str = RUN_DIR
    host: str = field(default_factory=socket.gethostname)
    dir_sizes: Callable = sysinfo.dir_sizes
    top_processes: Callable = sysinfo.top_processes


def sample_fast(prev, env, cfg, now):
    """The numbers /proc and statvfs give right now. `prev` is the "raw" part of the previous snapshot: cpu and
    network are rates between two runs, so the first run (or a counter that went backwards) leaves them None."""
    p, ts = env.proc, now.timestamp()
    cpu_now = sysinfo.read_cpu_times(os.path.join(p, "stat"))
    nets = sysinfo.net_bytes(os.path.join(p, "net", "dev"))
    net_now = (sum(v[0] for v in nets.values()), sum(v[1] for v in nets.values()))
    cpu = rx = tx = None
    prev = prev if isinstance(prev, dict) else {}
    if prev.get("cpu") and cpu_now[0] > prev["cpu"][0]:
        cpu = sysinfo.cpu_percent(tuple(prev["cpu"]), cpu_now)
    gap = ts - prev.get("t", ts)
    if prev.get("net") and gap > 0 and net_now[0] >= prev["net"][0] and net_now[1] >= prev["net"][1]:
        rx, tx = round((net_now[0] - prev["net"][0]) / gap), round((net_now[1] - prev["net"][1]) / gap)
    mem = sysinfo.meminfo(os.path.join(p, "meminfo"))
    loadfile = os.path.join(p, "loadavg")
    gone = set(cfg["exclude_mounts"])
    return {
        "t": core.iso(now), "cpu": cpu, "mem": mem["percent"] if mem["total"] else None,
        "swap": mem["swap_percent"] if mem["total"] else None,
        "load": list(sysinfo.loadavg(loadfile)) if os.path.exists(loadfile) else None,
        "ncpu": sysinfo.ncpu(), "uptime": sysinfo.uptime(os.path.join(p, "uptime")),
        "fs": {m["mount"]: sysinfo.fs_usage(m["mount"], env.statvfs)
               for m in sysinfo.mounts(os.path.join(p, "self", "mountinfo")) if m["mount"] not in gone},
        "rx": rx, "tx": tx, "raw": {"cpu": list(cpu_now), "net": list(net_now), "t": ts},
    }


def sample_units(cfg, env):
    """Service states ({unit: active|inactive|failed|unknown...}) and the failed units that are not ignored."""
    return {"svc": sysinfo.unit_states(cfg["services"], env.run),
            "failed": [u for u in sysinfo.failed_units(env.run) if u not in cfg["ignore_units"]]}


def checks_of(snap, cfg):
    """The values evaluate() judges: percent for resources, 1.0/0.0 for a service that is down/up, a count of failed
    units. None means "could not be measured", which changes nothing."""
    v = {"cpu": snap["cpu"], "memory": snap["mem"], "swap": snap["swap"]}
    for mount, u in snap["fs"].items():
        v[f"disk:{mount}"] = u["percent"] if u else None
        v[f"inodes:{mount}"] = u["inodes_percent"] if u else None
    for unit, state in snap["svc"].items():
        v[f"service:{unit}"] = None if state == "unknown" else float(state not in UP)
    v["failed_units"] = float(len(snap["failed"]))
    return v


# ---------------------------------------------------------------- alert state machine

def new_state():
    return {"checks": {}, "incident": None, "snap": {}, "last_notify": None, "last_summary": None, "pending": [], "closed": []}


def spec(key, cfg):
    """(warn, crit, sustain, margin) of a check; services and failed units are 0/1 style with no margin."""
    kind, t = key.partition(":")[0], cfg["thresholds"]
    if kind == "service":
        return INF, 1, t["services"]["sustain"], 0
    if kind == "failed_units":
        return 1, INF, t["failed_units"]["sustain"], 0
    return t[kind]["warn"], t[kind]["crit"], t[kind]["sustain"], MARGIN[kind]


def overall(state):
    return max((c["level"] for c in state["checks"].values()), default=0)


def raised(state):
    return {k: {"level": c["level"], "value": c["value"], "since": c["since"]} for k, c in state["checks"].items() if c["level"]}


def _step(c, v, sp, recover, t):
    """One sample for one check. Up needs `sustain` samples in a row at the threshold; down needs `recover` samples in
    a row below threshold minus margin, so a value hovering around the line cannot flap."""
    warn, crit, sustain, margin = sp
    nw = c["n"][0] + 1 if v >= warn else 0
    nc = c["n"][1] + 1 if v >= crit else 0
    up = 2 if nc >= sustain else 1 if nw >= sustain else 0
    level, down = c["level"], c["down"]
    if up > level:
        level, down = up, 0
        c["since"] = c["since"] or t
    else:
        calm = 2 if v >= crit - margin else 1 if v >= warn - margin else 0
        down = down + 1 if level > calm else 0
        if down >= recover:
            level, down = calm, 0
            if not level:
                c["since"] = None
    c.update(n=[nw, nc], level=level, down=down, value=v)


# A newer message makes older undelivered ones of these kinds pointless.
DROPS = {"open": (), "escalate": ALERTS, "reminder": ALERTS, "recover": (*ALERTS, "recover"), "summary": ("summary",)}


def evaluate(state, values, cfg, now):
    """(new state, events). Pure: the input is not touched and nothing is read or written.

    One mail when trouble starts (open), one when it gets worse (escalate), one reminder per notify_interval_hours
    while it lasts, one when it is over (recover), plus a summary every summary_every_days. An event waits in
    state["pending"] until delivered() says it went out."""
    s, t, events = copy.deepcopy(state), core.iso(now), []
    checks = s["checks"]
    for k in [k for k in checks if k not in values]:
        del checks[k]  # a mount or service that is no longer there
    for k, v in values.items():
        if v is None:
            continue
        c = checks.setdefault(k, {"n": [0, 0], "level": 0, "down": 0, "since": None, "value": None})
        _step(c, v, spec(k, cfg), cfg["recover_checks"], t)
    levels = {k: c["level"] for k, c in checks.items() if c["level"]}
    top, inc = max(levels.values(), default=0), s["incident"]

    def event(kind, level, keys, incident):
        return {"id": f"{t}-{kind}", "kind": kind, "t": t, "level": level,
                "checks": {k: {"level": checks[k]["level"], "value": checks[k]["value"], "since": checks[k]["since"]} for k in keys},
                "incident": incident, "next": None, "tries": 0, "error": None}

    if top and not inc:
        inc = s["incident"] = {"opened": t, "peak": top, "keys": sorted(levels)}
        s["snap"], s["last_notify"] = dict(levels), None
        events.append(event("open", top, levels, dict(inc)))
    elif not top and inc:
        done = dict(inc, closed=t)
        events.append(event("recover", 0, [k for k in inc["keys"] if k in checks], done))
        s["closed"] = (s["closed"] + [done])[-50:]
        s["incident"], s["snap"] = None, {}
    elif inc:
        inc["peak"] = max(inc["peak"], top)
        inc["keys"] = sorted({*inc["keys"], *levels})
        snap = s["snap"]
        worse = top > max(snap.values(), default=0) or any(v == 2 and snap.get(k) != 2 for k, v in levels.items())
        for k in list(snap):  # a level that dropped may escalate again later
            snap[k] = min(snap[k], levels.get(k, 0))
            if not snap[k]:
                del snap[k]
        last = core.parse_iso(s["last_notify"])
        quiet = not any(e["kind"] in ALERTS for e in s["pending"])
        if worse:
            events.append(event("escalate", top, levels, dict(inc)))
        elif quiet and last and timedelta(hours=cfg["notify_interval_hours"]) <= now - last:
            events.append(event("reminder", top, levels, dict(inc)))
        if events:
            s["snap"] = dict(levels)
    days = cfg["summary_every_days"]
    prev = core.parse_iso(s["last_summary"])
    if not days or prev is None:
        s["last_summary"] = t
    elif now - prev >= timedelta(days=days):
        events.append(event("summary", top, [], None))
        s["last_summary"] = t
    for e in events:
        s["pending"] = [p for p in s["pending"] if p["kind"] not in DROPS[e["kind"]]] + [e]
    return s, events


def delivered(state, ev_id, ok, now, detail=""):
    """The state after a send attempt: the event leaves the queue (a delivered alert also restarts the reminder
    clock) or waits RETRY before the next attempt."""
    s = copy.deepcopy(state)
    e = next((e for e in s["pending"] if e["id"] == ev_id), None)
    if e is None:
        return s
    if ok:
        s["pending"].remove(e)
        if e["kind"] in ALERTS and s["incident"]:
            s["last_notify"] = core.iso(now)
    else:
        e.update(tries=e["tries"] + 1, error=detail, next=core.iso(now + RETRY))
    return s


def due(state, now):
    return [e for e in state["pending"] if not e["next"] or core.parse_iso(e["next"]) <= now]


# ---------------------------------------------------------------- history (daily JSONL)

def append_history(hdir, rec, hcfg, now, statvfs=os.statvfs):
    """Append one record to today's file. None when written, else why recording is paused (never raises)."""
    day = now.strftime("%Y-%m-%d")
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    try:
        core.ensure_dir(hdir)
        try:
            s = statvfs(hdir)
            if s.f_bavail * s.f_frsize < MIN_FREE:
                return f"free space below {MIN_FREE >> 20} MiB"
        except OSError:
            pass  # cannot tell: try to write
        files = sorted(n for n in os.listdir(hdir) if DAY_RE.match(n))
        cutoff = (now - timedelta(days=hcfg["retention_days"])).strftime("%Y-%m-%d")
        for n in [n for n in files if n[:10] < cutoff]:
            os.unlink(os.path.join(hdir, n))
            files.remove(n)
        sizes = {n: os.path.getsize(os.path.join(hdir, n)) for n in files}
        cap = hcfg["max_mb"] << 20
        for n in files:  # oldest day first, never today's
            if sum(sizes.values()) + len(line) <= cap or n[:10] >= day:
                break
            os.unlink(os.path.join(hdir, n))
            del sizes[n]
        if sum(sizes.values()) + len(line) > cap:
            return f"history is over its {hcfg['max_mb']} MB cap"
        path = os.path.join(hdir, day + ".jsonl")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o640)
        try:
            size = os.fstat(fd).st_size
            torn = size > 0 and os.pread(fd, 1, size - 1) != b"\n"  # a crash mid-write left no newline
            os.write(fd, (("\n" if torn else "") + line).encode())
        finally:
            os.close(fd)
    except OSError as e:
        return e.strerror or str(e)
    return None


def read_history(hdir, since, until=None):
    """([records oldest first], number of unreadable lines or files). since and until are inclusive."""
    out, bad = [], 0
    try:
        names = sorted(n for n in os.listdir(hdir) if DAY_RE.match(n))
    except OSError:
        return [], 0
    first = (since - timedelta(days=1)).strftime("%Y-%m-%d")  # file names are local days: read one day too many
    last = (until + timedelta(days=1)).strftime("%Y-%m-%d") if until else "9999"
    for n in names:
        if not first <= n[:10] <= last:
            continue
        try:
            with open(os.path.join(hdir, n), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            bad += 1
            continue
        for line in filter(str.strip, lines):
            try:
                r = json.loads(line)
                t = core.parse_iso(r.get("t"))
            except (ValueError, AttributeError):
                t = None
            if t is None:
                bad += 1
            elif t >= since and (until is None or t <= until):
                out.append((t, r))
    out.sort(key=lambda x: x[0])
    return [r for _, r in out], bad


def history_due(last_iso, now, cfg, unhealthy, force=False):
    """Hourly when healthy, every 5 minutes in trouble; half a check of slack so a timer that is a few seconds early
    does not skip a slot. Always on a transition (force) or when the clock went backwards."""
    last = core.parse_iso(last_iso)
    if force or last is None:
        return True
    every = cfg["history"]["unhealthy_every_min" if unhealthy else "healthy_every_min"] * 60
    gap = (now - last).total_seconds()
    return gap < 0 or gap >= every - cfg["check_interval_sec"] / 2


def record_of(snap, level):
    return {"t": snap["t"], "lvl": ("ok", "warn", "crit")[level], "cpu": snap["cpu"], "mem": snap["mem"], "swap": snap["swap"],
            "load": snap["load"], "disk": {m: u["percent"] for m, u in snap["fs"].items() if u},
            "ino": {m: u["inodes_percent"] for m, u in snap["fs"].items() if u}, "rx": snap["rx"], "tx": snap["tx"],
            "failed": len(snap["failed"]), "down": [u for u, s in snap["svc"].items() if s not in UP and s != "unknown"]}


def history_stats(recs):
    """{metric: (min, avg, max)} over the records; a missing value is skipped, not counted as 0."""
    acc = {}

    def add(k, v):
        if isinstance(v, (int, float)):
            acc.setdefault(k, []).append(v)
    for r in recs:
        add("cpu", r.get("cpu")), add("memory", r.get("mem")), add("swap", r.get("swap"))
        for m, v in (r.get("disk") or {}).items():
            add(f"disk:{m}", v)
        for m, v in (r.get("ino") or {}).items():
            add(f"inodes:{m}", v)
    return {k: (min(v), sum(v) / len(v), max(v)) for k, v in acc.items()}


# ---------------------------------------------------------------- state files

def write_state(ctx, name, obj, run_dir):
    """True: written to the state dir. False: only the /run copy could be written. None: neither. Never raises."""
    for where, ok in ((ctx.state, True), (run_dir, False)):
        try:
            core.write_json(os.path.join(where, name), obj)
            return ok
        except OSError:
            continue
    return None


def read_state(ctx, name, default, run_dir):
    """The newest readable copy (the state dir wins a tie), else default."""
    found = []
    for rank, where in enumerate((ctx.state, run_dir)):
        path = os.path.join(where, name)
        try:
            found.append((-os.stat(path).st_mtime, rank, path))
        except OSError:
            pass
    for _, _, path in sorted(found):
        try:
            return core.read_json(path, default)
        except (core.Fail, OSError):
            continue
    return default


def read_alerts(ctx, run_dir):
    """The alert state with every key present (a fresh one when alerts.json is missing or not an object)."""
    state = read_state(ctx, "alerts.json", None, run_dir)
    return {**new_state(), **state} if isinstance(state, dict) else new_state()


# ---------------------------------------------------------------- mail text

def dur(sec):
    s = int(sec)
    if s < 60:
        return f"{s}s"
    m, h = s // 60, s // 3600
    if h < 1:
        return f"{m}m"
    return f"{h}h {m % 60}m" if h < 24 else f"{h // 24}d {h % 24}h"


def label(key):
    kind, _, rest = key.partition(":")
    return f"{kind} {rest}" if kind in ("disk", "inodes") else rest if kind == "service" else key.replace("_", " ")


def headline(key, v):
    kind = key.partition(":")[0]
    if kind == "service":
        return f"{label(key)} down"
    if kind == "failed_units":
        n = int(v or 0)
        return f"{n} failed unit{'' if n == 1 else 's'}"
    return f"{label(key)} {v or 0:.0f}%"


def _when(iso, now):
    t = core.parse_iso(iso)
    return "" if t is None else t.strftime("%H:%M" if t.date() == now.date() else "%m-%d %H:%M")


def _actions(key, snap):
    kind, _, rest = key.partition(":")
    q = shlex.quote(rest)
    if kind == "disk":
        return ["/usr/local/bin/synk-clean scan", f"/usr/bin/du -xh --max-depth=2 {q} | sort -rh | head -20"]
    if kind == "inodes":
        return [f"/usr/bin/du --inodes -x --max-depth=2 {q} | sort -rn | head -20"]
    if kind == "cpu":
        return ["/usr/bin/top -b -n1 -o %CPU | head -15"]
    if kind in ("memory", "swap"):
        return ["/usr/bin/free -h", "/usr/bin/swapon --show", "/usr/bin/ps aux --sort=-rss | head -15"]
    if kind == "service":
        return [f"/usr/bin/journalctl -u {q} -n 100 --no-pager", f"/usr/bin/systemctl restart {q}"]
    names = ((snap or {}).get("failed") or [])[:5]
    return ["/usr/bin/systemctl --failed"] + [f"/usr/bin/journalctl -u {shlex.quote(n)} -n 50 --no-pager" for n in names]


def render_mail(ev, cfg, host, snap, stats, extras, now):
    """(subject, body) of one event. Text from data; no I/O. `snap` may be None, `stats` is history_stats()."""
    kind, inc = ev["kind"], ev.get("incident")
    rows = sorted(ev["checks"].items(), key=lambda kv: (-kv[1]["level"], kv[0]))
    opened = core.parse_iso(inc["opened"]) if inc else None
    if kind == "recover":
        end = core.parse_iso(inc.get("closed") or ev["t"])
        length = dur((end - opened).total_seconds())
        subject = f"OK: recovered after {length} ({', '.join(label(k) for k in inc['keys'])})"
    elif kind == "summary":
        n = extras["incidents"]
        subject = "Weekly summary: " + (f"{n} incident{'' if n == 1 else 's'}" if n else "no incidents")
    elif kind == "report" or not rows:
        subject = f"Status report: {WORD[ev['level']]}"
    else:
        key, c = rows[0]
        tags = ([f"+{len(rows) - 1} more"] if len(rows) > 1 else []) + {"escalate": ["escalated"], "reminder": ["reminder"]}.get(kind, [])
        subject = f"{WORD[c['level']]}: {headline(key, c['value'])}" + (f" ({', '.join(tags)})" if tags else "")

    out = []
    if kind == "recover":
        out += [f"{host} is back to normal.", f"The incident lasted {length} (peak {WORD[inc['peak']]}), from {_when(inc['opened'], now)} "
                f"to {_when(inc.get('closed') or ev['t'], now)}."]
    elif kind == "summary":
        out += [f"Weekly summary for {host}: {extras['incidents']} incident(s) in the last 7 days."]
    else:
        status = WORD[ev["level"]] + (f", incident open for {dur((now - opened).total_seconds())}" if opened else "")
        out += [f"{host}: {status}"]
    out += ["", "Overview", f"  host      {host}"]
    if snap:
        pct = lambda v: "n/a" if v is None else f"{v:.0f}%"  # noqa: E731
        out.append(f"  now       cpu {pct(snap['cpu'])}  memory {pct(snap['mem'])}  swap {pct(snap['swap'])}"
                   + (f"  load {' '.join(f'{x:.2f}' for x in snap['load'])}" if snap.get("load") else ""))
        out.append("  disks     " + "  ".join(f"{m} {pct(u['percent']) if u else 'n/a'}" for m, u in snap["fs"].items()))
    if rows and kind != "recover":
        out += ["", "Affected"]
        for key, c in rows:
            since = f"  since {_when(c['since'], now)}" if c.get("since") else ""
            kind_ = key.partition(":")[0]
            if kind_ == "service":
                text = f"{label(key)} is not running"
            elif kind_ == "failed_units":
                names = ", ".join((snap or {}).get("failed") or [])
                text = headline(key, c["value"]) + (f": {names}" if names else "")
            else:
                warn, crit, _, _ = spec(key, cfg)
                text = f"{label(key)} {c['value']:.0f}%  warn {warn}%  crit {crit}%"
            out.append(f"  {WORD[c['level']]:<8}  {text}{since}")
    for mount, d in extras.get("dirs", {}).items():
        out += ["", f"Biggest directories on {mount}" + ("" if d.get("complete", True) else " (scan stopped early: sizes are lower bounds)")]
        out += [f"  {core.human(size):>10}  {os.path.join(mount, name)}" for name, size in d["entries"]]
    for by, procs in extras.get("procs", {}).items():
        out += ["", f"Busiest processes by {'cpu' if by == 'cpu' else 'memory'}"]
        out += [f"  {p['name']:<20} pid {p['pid']:<7} cpu {p['cpu']:.0f}%  memory {core.human(p['rss'])}" for p in procs]
    span = "Last 7 days" if kind == "summary" else "Last 24 hours"
    out += ["", f"{span} (min / avg / max, percent)"]
    out += [f"  {label(k):<18} {lo:.1f} / {avg:.1f} / {hi:.1f}" for k, (lo, avg, hi) in stats.items()] or ["  no history yet"]
    if kind == "summary":
        out += ["", f"Incidents: {extras['incidents']}"]
    if rows and kind not in ("recover", "summary"):
        out += ["", "Recommended actions"]
        for key, c in rows:
            out += [f"  {headline(key, c['value'])}"] + [f"    {a}" for a in _actions(key, snap)]
    out += ["", "More", "  /usr/local/bin/synk-dash            live view", "  /usr/local/bin/synk-monitor report  this report, now",
            "  Thresholds: etc/monitor/config.json under the management root."]
    return subject, "\n".join(out) + "\n"


# ---------------------------------------------------------------- one timer run

def _send(ctx, env, cfg, ev, snap, state, now):
    """Gather what the mail needs (best effort), render and send it. (ok, detail); never raises."""
    try:
        extras = {"dirs": {}, "procs": {}, "incidents": 0}
        if ev["kind"] in ALERTS:
            for key in [k for k in ev["checks"] if k.startswith("disk:")][:2]:
                extras["dirs"][key[5:]] = env.dir_sizes(key[5:], top=5, budget_s=8)
            for key, by in (("cpu", "cpu"), ("memory", "rss")):
                if key in ev["checks"]:
                    extras["procs"][by] = env.top_processes(5, by=by, proc=env.proc)
        week = ev["kind"] == "summary"
        if week:
            cut = now - timedelta(days=7)
            extras["incidents"] = sum(1 for c in state["closed"] if (t := core.parse_iso(c.get("closed"))) and t >= cut) + (1 if state["incident"] else 0)
        recs, _ = read_history(ctx.path("data", "history"), now - timedelta(days=7 if week else 1), now)
        subject, body = render_mail(ev, cfg, env.host, snap, history_stats(recs), extras, now)
        return env.send(ctx.root, subject, body)
    except Exception as e:  # noqa: BLE001 - a mail that cannot be built must be retried, not crash the timer run
        return False, f"{type(e).__name__}: {e}"


def check_once(ctx, env):
    """One timer run: sample, judge, record, mail what is due, write state. Returns the latest.json document.
    Raises core.Fail only for an invalid config; a full disk or a failing mail server never crashes it."""
    cfg, now = load(ctx), core.now()
    prev = read_state(ctx, "latest.json", None, env.run_dir)
    prev = prev if isinstance(prev, dict) else {}
    state = read_alerts(ctx, env.run_dir)
    old = prev.get("snap")
    snap = {**sample_fast((old or {}).get("raw") if isinstance(old, dict) else None, env, cfg, now), **sample_units(cfg, env)}
    state, events = evaluate(state, checks_of(snap, cfg), cfg, now)
    level, last, paused = overall(state), prev.get("last_record"), None
    if history_due(last, now, cfg, level > 0, any(e["kind"] in ("open", "recover") for e in events)):
        paused = append_history(ctx.path("data", "history"), record_of(snap, level), cfg["history"], now, env.statvfs)
        last = snap["t"] if paused is None else last
    stored = write_state(ctx, "alerts.json", state, env.run_dir)
    for e in events:
        core.log(ctx, "monitor." + e["kind"], level=e["level"], checks=sorted(e["checks"]))
    for e in due(state, now):
        ok, detail = _send(ctx, env, cfg, e, snap, state, now)
        if not ok and not e["tries"]:
            core.log(ctx, "monitor.mail_failed", kind=e["kind"], error=detail)
        state = delivered(state, e["id"], ok, now, detail)
        stored = write_state(ctx, "alerts.json", state, env.run_dir)
    doc = {"t": snap["t"], "level": level, "interval": cfg["check_interval_sec"], "snap": snap, "last_record": last,
           "history": paused, "stored": stored is True}
    write_state(ctx, "latest.json", doc, env.run_dir)
    return doc
