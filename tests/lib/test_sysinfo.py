"""Host-fact parsers on recorded /proc and systemd text; readers on temp dirs and stub runners."""
import itertools
import os
import unittest
from unittest import mock

from libtest import PROC, Base, fixture, wr

from softsynk import core, sysinfo

KB = 1024


def runner(text, rc=0, calls=None):
    def fake(argv, **kw):
        if calls is not None:
            calls.append(argv)
        return core.Result(rc, text, "")
    return fake


class Parsers(unittest.TestCase):
    def test_meminfo_in_bytes_with_and_without_swap(self):
        m = sysinfo.parse_meminfo(fixture("meminfo"))
        self.assertEqual((m["total"], m["available"], m["used"]), (8040000 * KB, 2010000 * KB, 6030000 * KB))
        self.assertEqual((m["percent"], m["swap_total"], m["swap_used"], m["swap_percent"]), (75.0, 2000000 * KB, 500000 * KB, 25.0))
        n = sysinfo.parse_meminfo(fixture("meminfo-noswap"))
        self.assertEqual((n["swap_total"], n["swap_used"], n["swap_percent"], n["percent"]), (0, 0, 0.0, 60.0))

    def test_cpu_times_and_percent_between_samples(self):
        self.assertEqual(sysinfo.cpu_times(fixture("stat")), (22785, 16770))
        self.assertEqual(sysinfo.cpu_percent((1000, 800), (1200, 900)), 50.0)
        self.assertEqual(sysinfo.cpu_percent((1000, 800), (1000, 800)), 0.0)  # no time passed: no division by zero

    def test_load_uptime_ncpu(self):
        self.assertEqual(sysinfo.parse_loadavg(fixture("loadavg")), (0.52, 0.48, 0.40))
        self.assertEqual(sysinfo.parse_uptime(fixture("uptime")), 3600.55)
        self.assertGreaterEqual(sysinfo.ncpu(), 1)

    def test_mounts_keep_real_filesystems_once_and_decode_escapes(self):
        got = [(m["mount"], m["fstype"], m["source"]) for m in sysinfo.parse_mounts(fixture("mountinfo"))]
        self.assertEqual(got, [("/", "ext4", "/dev/sda1"), ("/mnt/data disk", "xfs", "/dev/sdb"), ("/boot/efi", "vfat", "/dev/sda15")])

    def test_net_dev_skips_loopback_and_virtual_interfaces(self):
        self.assertEqual(sysinfo.parse_net_dev(fixture("net-dev")),
                         {"eth0": (1000000, 500000), "eth1": (200000, 100000)})

    def test_listeners_from_ss(self):
        got = sysinfo.parse_ss(fixture("ss"))
        by = {(g["addr"], g["port"]): g for g in got}
        self.assertEqual(len(got), 7)
        self.assertEqual(by[("0.0.0.0", 22)]["procs"], ["sshd"])
        self.assertFalse(by[("0.0.0.0", 22)]["local"])
        self.assertEqual(by[("0.0.0.0", 80)]["procs"], ["nginx"])  # two workers, one name
        self.assertTrue(by[("127.0.0.1", 6379)]["local"])
        self.assertEqual(by[("127.0.0.53", 53)]["procs"], [])  # %lo scope stripped, no process info
        self.assertEqual(by[("::", 22)]["local"], False)
        self.assertTrue(by[("::1", 5432)]["local"])
        self.assertEqual(by[("*", 3306)]["port"], 3306)

    def test_unit_lists_strip_bullets_and_columns(self):
        self.assertEqual(sysinfo.parse_units(fixture("systemctl-failed")), ["logrotate.service", "softsynk-backup.service"])
        self.assertEqual(len(sysinfo.parse_units(fixture("systemctl-running"))), 5)
        self.assertEqual(sysinfo.parse_units(""), [])


class Readers(Base):
    def test_fs_usage_is_df_style_for_blocks_and_inodes(self):
        vfs = mock.Mock(f_frsize=4096, f_blocks=1000, f_bfree=300, f_bavail=200, f_files=100, f_ffree=40)
        u = sysinfo.fs_usage("/x", statvfs=lambda p: vfs)
        self.assertEqual((u["total"], u["used"], u["free"]), (1000 * 4096, 700 * 4096, 200 * 4096))
        self.assertEqual((u["percent"], u["inodes_percent"]), (77.8, 60.0))  # used / (used + available), as df
        vfs.f_files = 0  # filesystems without inodes (btrfs): 0, not a crash
        self.assertEqual(sysinfo.fs_usage("/x", statvfs=lambda p: vfs)["inodes_percent"], 0.0)
        def gone(p):
            raise OSError("gone")
        self.assertIsNone(sysinfo.fs_usage("/x", statvfs=gone))

    def test_top_processes_by_rss_and_by_cpu(self):
        with mock.patch.object(sysinfo, "_HZ", 100), mock.patch.object(sysinfo, "_PAGE", 4096):
            rss = sysinfo.top_processes(2, by="rss", proc=str(PROC / "pids"))
            cpu = sysinfo.top_processes(5, by="cpu", proc=str(PROC / "pids"))
        self.assertEqual([p["pid"] for p in rss], [101, 202])
        self.assertEqual(rss[0], {"pid": 101, "name": "nginx", "rss": 5000 * 4096, "cpu": 0.2})
        self.assertEqual([(p["pid"], p["name"], p["cpu"]) for p in cpu],
                         [(202, "my (odd) proc", 10.2), (101, "nginx", 0.2), (303, "cron", 0.0)])

    def test_top_processes_skips_vanished_and_garbled_entries(self):
        proc = self.tmp / "proc"
        (proc / "5").mkdir(parents=True)
        wr(proc / "5" / "stat", "5 (x) S garbage")
        (proc / "6").mkdir()  # exited between listdir and read
        (proc / "self").mkdir()
        wr(proc / "uptime", "100.0 90.0")
        self.assertEqual(sysinfo.top_processes(5, proc=str(proc)), [])

    def test_php_versions_sort_newest_first_with_their_sapis(self):
        base = self.tmp / "php"
        for d in ("8.2/cli", "8.3/cli", "8.3/fpm", "8.10/cli", "mods-available", "7.4"):
            (base / d).mkdir(parents=True)
        self.assertEqual(sysinfo.php_versions(str(base)), {"8.10": ["cli"], "8.3": ["cli", "fpm"], "8.2": ["cli"], "7.4": []})
        self.assertEqual(sysinfo.php_versions(str(self.tmp / "none")), {})

    def test_dir_sizes_ranks_children_and_does_not_follow_symlinks(self):
        d = self.tmp / "tree"
        (d / "big").mkdir(parents=True)
        (d / "small" / "deep").mkdir(parents=True)
        wr(d / "big" / "f", "x" * 300_000)
        wr(d / "small" / "deep" / "f", "x" * 20_000)
        wr(d / "loose", "x" * 100_000)
        os.symlink(str(d), str(d / "small" / "loop"))
        r = sysinfo.dir_sizes(str(d), top=2)
        self.assertEqual([n for n, _ in r["entries"]], ["big", "loose"])
        self.assertTrue(r["complete"])
        self.assertGreaterEqual(r["total"], 420_000)
        self.assertLess(r["total"], 600_000)  # the symlinked loop did not double it

    def test_dir_sizes_stops_at_its_time_budget_and_says_so(self):
        d = self.tmp / "tree"
        for i in range(5):
            (d / f"d{i}").mkdir(parents=True)
            wr(d / f"d{i}" / "f", "x")
        tick = itertools.count()
        r = sysinfo.dir_sizes(str(d), budget_s=3, clock=lambda: next(tick))
        self.assertFalse(r["complete"])
        self.assertLess(len(r["entries"]), 5)
        self.assertTrue(sysinfo.dir_sizes(str(d))["complete"])

    def test_dir_sizes_of_a_missing_path_is_empty_not_an_error(self):
        r = sysinfo.dir_sizes(str(self.tmp / "none"))
        self.assertEqual((r["entries"], r["total"]), ([], 0))


class Systemd(unittest.TestCase):
    def test_running_services_filtered_by_glob(self):
        calls = []
        run = runner(fixture("systemctl-running"), calls=calls)
        self.assertEqual(sysinfo.running_services(("php*-fpm.service", "nginx.service"), run=run), ["nginx.service", "php8.3-fpm.service"])
        self.assertEqual(len(sysinfo.running_services(run=run)), 5)  # no patterns: all
        self.assertEqual(calls[0][0], "systemctl")

    def test_unit_states_pair_up_by_order_and_degrade_to_unknown(self):
        units = ["a.service", "b.service", "c.service", "d.service"]
        self.assertEqual(sysinfo.unit_states(units, run=runner(fixture("systemctl-is-active"), rc=3)),
                         {"a.service": "active", "b.service": "inactive", "c.service": "failed", "d.service": "activating"})
        self.assertEqual(sysinfo.unit_states(units, run=runner("active\n", rc=3))["b.service"], "unknown")  # short output
        self.assertEqual(sysinfo.unit_states([], run=runner("")), {})

    def test_failed_units_and_missing_systemctl(self):
        self.assertEqual(sysinfo.failed_units(run=runner(fixture("systemctl-failed"))), ["logrotate.service", "softsynk-backup.service"])
        gone = runner("", rc=127)
        self.assertEqual((sysinfo.failed_units(run=gone), sysinfo.running_services(run=gone), sysinfo.listeners(run=gone)), ([], [], []))


if __name__ == "__main__":
    unittest.main()
