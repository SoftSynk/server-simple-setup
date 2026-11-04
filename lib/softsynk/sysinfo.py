"""Host facts from /proc, statvfs and systemd, as plain data.

Parsers take text and are pure; readers wrap them with a path or a runner (default core.run) that tests
replace. Anything that cannot be read gives an empty or None result, never an exception: a tool must
keep working on a host that lacks systemd, ss or a given /proc file.
"""
import fnmatch
import os
import re
import time

from . import core

_HZ = os.sysconf("SC_CLK_TCK")
_PAGE = os.sysconf("SC_PAGE_SIZE")
# Filesystems that live on a disk of ours; tmpfs, overlay, squashfs, autofs and friends are not capacity.
REAL_FS = frozenset({"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs", "vfat", "ntfs", "exfat"})
VIRTUAL_NICS = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "cni", "flannel", "cali")


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else 0.0


# ---------------------------------------------------------------- memory, cpu, load, uptime

def parse_meminfo(text):
    """Bytes: total, available, used (= total - available), percent; swap_total, swap_used, swap_percent."""
    kb = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts and parts[0].isdigit():
            kb[k] = int(parts[0]) * 1024
    total, avail = kb.get("MemTotal", 0), kb.get("MemAvailable", kb.get("MemFree", 0))
    swap, swap_free = kb.get("SwapTotal", 0), kb.get("SwapFree", 0)
    return {"total": total, "available": avail, "used": total - avail, "percent": _pct(total - avail, total),
            "swap_total": swap, "swap_used": swap - swap_free, "swap_percent": _pct(swap - swap_free, swap)}


def meminfo(path="/proc/meminfo"):
    return parse_meminfo(_read(path))


def cpu_times(text):
    """(total, idle) jiffies from the aggregate "cpu" line of /proc/stat; idle includes iowait."""
    for line in text.splitlines():
        f = line.split()
        if f and f[0] == "cpu":
            v = [int(x) for x in f[1:9]]  # guest is already inside user; ignore it
            return sum(v), v[3] + v[4]
    return 0, 0


def cpu_percent(prev, cur):
    """Busy percent between two cpu_times() samples; 0 when no time passed."""
    total = cur[0] - prev[0]
    return round(100.0 * (total - (cur[1] - prev[1])) / total, 1) if total > 0 else 0.0


def read_cpu_times(path="/proc/stat"):
    return cpu_times(_read(path))


def parse_loadavg(text):
    f = text.split()
    return tuple(float(x) for x in f[:3]) if len(f) >= 3 else (0.0, 0.0, 0.0)


def loadavg(path="/proc/loadavg"):
    return parse_loadavg(_read(path))


def ncpu():
    return os.cpu_count() or 1


def parse_uptime(text):
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return 0.0


def uptime(path="/proc/uptime"):
    return parse_uptime(_read(path))


# ---------------------------------------------------------------- disks and network

def _unescape(s):
    """mountinfo writes space, tab, newline and backslash as \\040 style octal."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def parse_mounts(text):
    """Mounts of real disk filesystems, each device once (bind mounts and subvolumes repeat a device).
    [{"mount", "fstype", "source", "dev"}], in mountinfo order."""
    seen, out = set(), []
    for line in text.splitlines():
        pre, sep, post = line.partition(" - ")
        a, b = pre.split(), post.split()
        if not sep or len(a) < 5 or len(b) < 2 or b[0] not in REAL_FS or a[2] in seen:
            continue
        seen.add(a[2])
        out.append({"mount": _unescape(a[4]), "fstype": b[0], "source": _unescape(b[1]), "dev": a[2]})
    return out


def mounts(path="/proc/self/mountinfo"):
    return parse_mounts(_read(path))


def fs_usage(mount, statvfs=os.statvfs):
    """Capacity of one mount as df shows it (percent = used / (used + available)), plus inode use.
    None when the mount cannot be read."""
    try:
        s = statvfs(mount)
    except OSError:
        return None
    used = (s.f_blocks - s.f_bfree) * s.f_frsize
    free = s.f_bavail * s.f_frsize
    inodes_used = s.f_files - s.f_ffree
    return {"mount": mount, "total": s.f_blocks * s.f_frsize, "used": used, "free": free, "percent": _pct(used, used + free),
            "inodes_total": s.f_files, "inodes_used": inodes_used, "inodes_percent": _pct(inodes_used, s.f_files)}


def parse_net_dev(text):
    """{interface: (rx_bytes, tx_bytes)} without loopback and container or virtual interfaces."""
    out = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        f = rest.split()
        if sep and len(f) >= 9 and f[0].isdigit() and not name.strip().startswith(VIRTUAL_NICS):
            out[name.strip()] = (int(f[0]), int(f[8]))
    return out


def net_bytes(path="/proc/net/dev"):
    return parse_net_dev(_read(path))


# ---------------------------------------------------------------- processes

def top_processes(n=5, by="rss", proc="/proc"):
    """The n biggest processes by resident memory ("rss") or lifetime-average cpu ("cpu"):
    [{"pid", "name", "rss" bytes, "cpu" percent of one core}]. Processes that vanish mid-read are skipped."""
    up = parse_uptime(_read(os.path.join(proc, "uptime")))
    out = []
    try:
        names = os.listdir(proc)
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        text = _read(os.path.join(proc, name, "stat"))
        i, j = text.find("("), text.rfind(")")  # the name may itself contain parentheses
        f = text[j + 1:].split()
        if i < 0 or j < i or len(f) < 22:
            continue
        try:
            busy_s = (int(f[11]) + int(f[12])) / _HZ
            alive_s = up - int(f[19]) / _HZ
            out.append({"pid": int(name), "name": text[i + 1:j], "rss": int(f[21]) * _PAGE,
                        "cpu": round(100.0 * busy_s / alive_s, 1) if alive_s > 0 else 0.0})
        except ValueError:
            continue
    out.sort(key=lambda p: p["cpu" if by == "cpu" else "rss"], reverse=True)
    return out[:n]


# ---------------------------------------------------------------- systemd and sockets

def parse_units(text):
    """Unit names from `systemctl list-units --no-legend` output (a leading bullet marks failed units)."""
    out = []
    for line in text.splitlines():
        f = line.lstrip(" \t●*").split()
        if f:
            out.append(f[0])
    return out


def _systemctl(run, *args):
    r = run(["systemctl", *args, "--no-pager", "--no-legend", "--plain"], timeout=20)
    return parse_units(r.out) if r.rc == 0 else []


def running_services(patterns=(), run=core.run):
    """Running service units, optionally only those matching one of the glob patterns."""
    units = _systemctl(run, "list-units", "--type=service", "--state=running")
    return [u for u in units if not patterns or any(fnmatch.fnmatchcase(u, p) for p in patterns)]


def failed_units(run=core.run):
    return _systemctl(run, "list-units", "--failed")


def unit_states(units, run=core.run):
    """{unit: active|inactive|failed|activating|...} in one call; "unknown" for any the output does not cover
    (is-active exits non-zero when any unit is not active, so the exit code is not an error here)."""
    if not units:
        return {}
    lines = run(["systemctl", "is-active", "--", *units], timeout=20).out.split()
    return {u: lines[i] if len(lines) == len(units) else "unknown" for i, u in enumerate(units)}


def parse_ss(text):
    """Listening TCP sockets from `ss -H -ltnp`: [{"addr", "port", "procs": [names], "local": loopback only}]."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 5 or f[0] != "LISTEN":
            continue
        addr, _, port = f[3].rpartition(":")
        addr = addr.strip("[]").partition("%")[0]
        if not port.isdigit():
            continue
        procs = list(dict.fromkeys(re.findall(r'\("([^"]+)"', line)))
        out.append({"addr": addr, "port": int(port), "procs": procs, "local": addr.startswith("127.") or addr == "::1"})
    return out


def listeners(run=core.run):
    r = run(["ss", "-H", "-ltnp"], timeout=20)
    return parse_ss(r.out) if r.rc == 0 else []


# ---------------------------------------------------------------- packages and files

def php_versions(base="/etc/php"):
    """{"8.3": ["cli", "fpm"], ...} for each PHP version configured under /etc/php, newest first."""
    try:
        names = [n for n in os.listdir(base) if re.fullmatch(r"\d+\.\d+", n)]
    except OSError:
        return {}
    names.sort(key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    return {v: sorted(s for s in ("cli", "fpm", "apache2") if os.path.isdir(os.path.join(base, v, s))) for v in names}


def dir_sizes(path, top=10, budget_s=5, clock=time.monotonic):
    """Disk use of the immediate children of `path`, largest first, like du --max-depth=1.

    Stays on one filesystem, follows no symlinks and stops after budget_s seconds (complete=False then,
    and the sizes are lower bounds). Unreadable directories are counted in "errors". Blocks, not byte
    lengths, so sparse files are not overstated; hard links count once per name (ponytail: good enough for ranking).
    Returns {"entries": [(name, bytes)], "total", "complete", "errors"}.
    """
    start = clock()
    sizes, errors, complete = {}, 0, True
    try:
        dev = os.lstat(path).st_dev
    except OSError:
        return {"entries": [], "total": 0, "complete": True, "errors": 1}
    stack = [(path, None)]
    while stack and complete:
        cur, top_name = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            errors += 1
            continue
        with it:
            for e in it:
                if clock() - start > budget_s:
                    complete = False
                    break
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    errors += 1
                    continue
                if st.st_dev != dev:
                    continue
                key = top_name or e.name
                sizes[key] = sizes.get(key, 0) + st.st_blocks * 512
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, key))
    ranked = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
    return {"entries": ranked[:top], "total": sum(sizes.values()), "complete": complete, "errors": errors}
