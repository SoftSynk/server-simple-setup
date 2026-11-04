"""synk-tune: capacity rules, config parsing, the OPcache drop-in apply/rollback and the status document.

In-process on a temp tree (Host(fsroot)) with a scripted command runner; the clock is SOFTSYNK_NOW.
One subprocess smoke test runs the real script. No live server, no real systemctl or php-fpm.
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from libtest import REPO, Base, rd  # noqa: E402

from softsynk import change, core, ui  # noqa: E402

SCRIPT = str(REPO / "deployment" / "optimizer" / "synk-tune")


def load_script():
    loader = importlib.machinery.SourceFileLoader("synk_tune", SCRIPT)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("synk_tune", loader))
    loader.exec_module(mod)
    return mod


tune = load_script()
MB = 1024 * 1024
DROPIN = "/etc/php/8.3/fpm/conf.d/99-softsynk-opcache.ini"
FPM_LOG_NOW = "[04-Nov-2026 10:15:23] WARNING: [pool www] server reached pm.max_children setting (5), consider raising it\n"


class Runner:
    """Stands in for core.run. rcs maps argv[0] to the exit codes of successive calls (the last one repeats)."""

    def __init__(self, units=(), rcs=None):
        self.units, self.rcs, self.calls = list(units), rcs or {}, []

    def __call__(self, argv, timeout=None, input=None, env=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["systemctl", "list-units"]:
            return core.Result(0, "".join(f"{u} loaded active running x\n" for u in self.units))
        if argv[:2] == ["systemctl", "is-active"]:
            return core.Result(0, "active\n")
        seq = self.rcs.get(argv[0])
        rc = (seq.pop(0) if len(seq) > 1 else seq[0]) if seq else 0
        return core.Result(rc, "", "boom\n" if rc else "")

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


class Fs(Base):
    """A fake server: files under self.fs, processes under self.fs/proc."""

    def setUp(self):
        super().setUp()
        self.fs = self.tmp / "fs"
        self.host = tune.Host(str(self.fs), cpus=2)
        self.ctx = core.Ctx("optimizer", str(self.root))
        self.pid = 100
        self.runner = Runner()
        env = {k: v for k, v in os.environ.items() if not k.startswith("SOFTSYNK_")}
        env["SOFTSYNK_NONINTERACTIVE"] = "1"
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(ui.setup)
        self.at("2026-11-04T12:00:00")

    def put(self, path, text=""):
        p = self.fs / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def mem(self, mb):
        self.put("/proc/meminfo", f"MemTotal: {mb * 1024} kB\nMemAvailable: {mb * 512} kB\n")

    def proc(self, comm, cmd=None, mb=50, limits=None, environ=None, exact=True):
        self.pid += 1
        d = f"/proc/{self.pid}"
        self.put(f"{d}/comm", comm + "\n")
        self.put(f"{d}/cmdline", cmd or comm)
        if exact:
            self.put(f"{d}/smaps_rollup", f"Rss: {mb * 1024} kB\nPss:             {mb * 1024} kB\n")
        else:
            self.put(f"{d}/statm", f"1000 {mb * MB // os.sysconf('SC_PAGE_SIZE')} 0 0 0 0 0\n")
        if limits:
            self.put(f"{d}/limits", limits)
        if environ:
            self.put(f"{d}/environ", environ)
        return self.pid

    def php(self, ver="8.3", pool="pm = dynamic\npm.max_children = 5\npm.max_requests = 500\n", ini="", opcache=True, name="www"):
        self.put(f"/etc/php/{ver}/fpm/php.ini", ini)
        self.put(f"/etc/php/{ver}/cli/php.ini", "")
        if opcache:
            self.put(f"/etc/php/{ver}/fpm/conf.d/10-opcache.ini", "zend_extension=opcache.so\n")
        self.put(f"/etc/php/{ver}/fpm/pool.d/{name}.conf", f"[{name}]\n{pool}")
        self.runner.units.append(f"php{ver}-fpm.service")

    def workers(self, ver, n, mb, name="www"):
        self.proc(f"php-fpm{ver}", f"php-fpm: master process (/etc/php/{ver}/fpm/php-fpm.conf)", mb=20)
        for _ in range(n):
            self.proc(f"php-fpm{ver}", f"php-fpm: pool {name}", mb=mb)

    def neighbours(self):
        self.proc("nginx", "nginx: master process /usr/sbin/nginx", mb=30)
        self.proc("redis-server", mb=100)
        self.proc("mysqld", mb=800)

    def audit(self):
        return tune.audit(self.ctx, self.host, self.runner)

    def finding(self, fid):
        return next((f for f in self.audit()["findings"] if f.id == fid), None)

    def ids(self):
        return [f.id for f in self.audit()["findings"]]

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = core.main(lambda: tune.main(["--root", str(self.root), "--non-interactive", *argv], host=self.host, run=self.runner))
        return rc, out.getvalue(), err.getvalue()


class TestSizing(unittest.TestCase):
    def test_parse_size(self):
        p = tune.parse_size
        self.assertEqual((p("128M"), p("1G"), p("512k"), p("64m")), (128 * MB, 1024 * MB, 512 * 1024, 64 * MB))
        self.assertEqual((p("-1"), p("0"), p("2048")), (-1, 0, 2048))
        self.assertIsNone(p("lots"))

    def test_budget_keeps_a_reserve_and_the_neighbours(self):
        self.assertAlmostEqual(tune.budget_mb(4096, 930), 4096 - 512 - 1.2 * 930)  # 10% is below the 512 MB floor
        self.assertAlmostEqual(tune.budget_mb(16384, 0), 16384 * 0.9)
        self.assertAlmostEqual(tune.budget_mb(2048, 0), 2048 - 512)  # the floor of the reserve is 512 MB

    def test_children_follow_the_budget_within_limits(self):
        s = tune.size_children
        self.assertEqual(s(2570, 75, 5), 10)    # RAM allows 34; never more than double the current value
        self.assertEqual(s(2570, 75, 60), 34)   # RAM is the limit
        self.assertEqual(s(200, 75, 5), 2)      # 2.67 -> 2
        self.assertEqual(s(10, 75, 5), 2)       # floor
        self.assertEqual(s(-300, 75, 5), 2)     # neighbours already use more than the budget
        self.assertEqual(s(5000, 10, 1), 2)     # a pool of 1 may grow to 2, not to 500

    def test_spare_servers_stay_ordered(self):
        self.assertEqual(tune.spare_servers(2), (1, 1, 2))
        self.assertEqual(tune.spare_servers(10), (3, 3, 5))
        self.assertEqual(tune.spare_servers(40), (10, 10, 20))
        for n in range(2, 300):
            start, lo, hi = tune.spare_servers(n)
            self.assertTrue(1 <= lo <= start <= hi <= n, n)

    def test_worker_size_is_measured_or_assumed(self):
        self.assertEqual(tune.worker_mb([60, 60, 60], 64), (75.0, True))   # busy workers grow: 25% headroom
        self.assertEqual(tune.worker_mb([60], 64), (64, False))
        self.assertEqual(tune.worker_mb([], 40), (40, False))


class TestParsing(Fs):
    def test_effective_ini_last_file_wins_and_names_its_source(self):
        self.put("/etc/php/8.3/fpm/php.ini", "[PHP]\n; comment\nmemory_limit = 128M ; inline\nupload_max_filesize = \"2M\"\n")
        self.put("/etc/php/8.3/fpm/conf.d/10-opcache.ini", "zend_extension=opcache.so\n")
        self.put("/etc/php/8.3/fpm/conf.d/20-x.ini", "memory_limit=512M\n")
        entries = tune.ini_entries(self.host, tune.php_ini_files(self.host, "8.3"))
        eff = tune.effective(entries)
        self.assertEqual(eff["memory_limit"], ("512M", "/etc/php/8.3/fpm/conf.d/20-x.ini"))
        self.assertEqual(eff["upload_max_filesize"][0], "2M")
        self.assertTrue(tune.opcache_loaded(entries))
        self.assertFalse(tune.opcache_loaded([("zend_extension", "xdebug.so", "x")]))

    def test_pools(self):
        self.put("/etc/php/8.3/fpm/pool.d/www.conf", "[www]\nuser = www-data\n;pm.max_children = 99\npm = dynamic\npm.max_children = 12 ; x\n\n[api]\npm = static\n")
        pools = tune.fpm_pools(self.host, "8.3")
        self.assertEqual([p["name"] for p in pools], ["www", "api"])
        self.assertEqual(pools[0]["conf"]["pm.max_children"], "12")
        self.assertEqual(pools[1]["conf"], {"pm": "static"})

    def test_saturation_counts_only_the_window(self):
        self.put("/var/log/php8.3-fpm.log",
                 "[01-Nov-2026 08:00:00] WARNING: [pool www] server reached pm.max_children setting (5), consider raising it\n"
                 + FPM_LOG_NOW + FPM_LOG_NOW.replace("10:15:23", "10:16:00")
                 + "[04-Nov-2026 11:00:00] WARNING: [pool api] server reached pm.max_children setting (3), consider raising it\n"
                 "[04-Nov-2026 11:30:00] NOTICE: ready to handle connections\n"
                 "[01-Jan-2020 00:00:00.123456] WARNING: [pool www] server reached pm.max_children setting (5)\n")
        self.assertEqual(tune.saturation(self.host, "8.3", 24), {"www": 2, "api": 1})
        self.assertEqual(tune.saturation(self.host, "8.3", 24 * 7), {"www": 3, "api": 1})
        self.assertEqual(tune.saturation(self.host, "8.4", 24), {})

    def test_fpm_log_path_comes_from_php_fpm_conf(self):
        self.put("/etc/php/8.3/fpm/php-fpm.conf", ";error_log = /var/log/commented.log\nerror_log = /srv/fpm.log\n")
        self.assertEqual(tune.fpm_log(self.host, "8.3"), "/srv/fpm.log")
        self.assertEqual(tune.fpm_log(self.host, "8.4"), "/var/log/php8.4-fpm.log")

    def test_nginx_settings_follow_includes_and_ignore_comments(self):
        self.put("/etc/nginx/nginx.conf", "worker_processes 1; # auto\nevents { worker_connections 4096; }\n"
                 "http {\n gzip off;\n client_max_body_size 1m;\n include /etc/nginx/conf.d/*.conf;\n include sites/*;\n}\n")
        self.put("/etc/nginx/conf.d/a.conf", "server { client_max_body_size 64M; # gzip_types x\n}\n")
        self.put("/etc/nginx/sites/b", "server { gzip_types text/css; }\n")
        s = tune.nginx_settings(self.host)
        self.assertEqual((s["worker_processes"], s["worker_connections"], s["gzip"], s["body"]), ("1", 4096, "off", 64 * MB))
        self.assertTrue(s["gzip_types"])
        self.assertIsNone(s["rlimit"])
        self.assertIsNone(tune.nginx_settings(tune.Host(str(self.tmp / "nowhere"))))

    def test_apache_mpm_and_defaults(self):
        self.put("/etc/apache2/mods-enabled/mpm_prefork.load", "LoadModule mpm_prefork_module x\n")
        self.put("/etc/apache2/mods-enabled/mpm_prefork.conf", "<IfModule mpm_prefork_module>\n  MaxRequestWorkers 80\n</IfModule>\n")
        self.assertEqual(tune.apache_settings(self.host), {"mpm": "prefork", "max_workers": 80, "threads": 25})
        self.assertEqual(tune.apache_settings(tune.Host(str(self.tmp / "nowhere"))), None)

    def test_process_scan_prefers_pss_and_falls_back_to_rss(self):
        self.proc("nginx", "nginx: master process", mb=30)
        self.proc("php", "php artisan queue:work", mb=70, exact=False)
        procs = {p["comm"]: p for p in tune.scan_procs(self.host)}
        self.assertEqual((procs["nginx"]["pss"], procs["nginx"]["exact"]), (30 * MB, True))
        self.assertEqual((procs["php"]["pss"], procs["php"]["exact"]), (70 * MB, False))
        self.assertEqual(procs["php"]["cmd"], "php artisan queue:work")


class TestCount(Fs):
    def test_counts_app_php_files_once(self):
        self.put("/var/www/site/artisan")
        for f in ("app/a.php", "vendor/v/b.php", "vendor/v/readme.md", ".git/c.php", "node_modules/d.php"):
            self.put("/var/www/site/" + f)
        self.put("/outside/x.php")
        os.symlink(self.fs / "outside", self.fs / "var/www/site/linked")
        os.symlink(self.fs / "var/www/site", self.fs / "var/www/alias")
        self.put("/home/forge/zd/releases/1/artisan")
        self.put("/home/forge/zd/releases/1/app/x.php")
        self.put("/home/forge/zd/releases/0/old.php")
        os.symlink("releases/1", self.fs / "home/forge/zd/current")
        self.put("/home/forge/notes/readme.php")  # not an app: no marker file
        self.assertEqual(tune.count_php_files(self.host), (3, True))

    def test_stops_at_the_limit(self):
        self.put("/var/www/site/index.php")
        for i in range(4):
            self.put(f"/var/www/site/f{i}.php")
        self.assertEqual(tune.count_php_files(self.host, limit=2), (2, False))

    def test_no_apps(self):
        self.assertEqual(tune.count_php_files(self.host), (0, True))


class TestOpcache(Fs):
    def setUp(self):
        super().setUp()
        self.mem(4096)
        self.php()
        count = mock.patch.object(tune, "count_php_files", return_value=(20000, True))
        self.count = count.start()
        self.addCleanup(count.stop)

    def test_wanted_by_file_count(self):
        w = tune.opcache_wanted
        self.assertEqual(w(3000), {"opcache.memory_consumption": 128, "opcache.interned_strings_buffer": 8, "opcache.max_accelerated_files": 16229})
        self.assertEqual(w(12000)["opcache.max_accelerated_files"], 16229)   # 15600 needed
        self.assertEqual(w(13000)["opcache.max_accelerated_files"], 32531)
        self.assertEqual(w(20000), {"opcache.memory_consumption": 256, "opcache.interned_strings_buffer": 16, "opcache.max_accelerated_files": 32531})
        self.assertEqual(w(40000), {"opcache.memory_consumption": 512, "opcache.interned_strings_buffer": 32, "opcache.max_accelerated_files": 65407})
        self.assertEqual(w(900000)["opcache.max_accelerated_files"], 1000000)  # the highest value PHP accepts

    def test_prime_slots(self):
        self.assertEqual([tune.prime_slots(n) for n in (10000, 16229, 16230, 70000)], [16229, 16229, 32531, 130987])

    def test_defaults_are_raised_to_the_wanted_values(self):
        got = {f.id: f.proposed for f in self.audit()["findings"] if tune.applicable(f)}
        self.assertEqual(got, {"opcache/8.3/opcache.memory_consumption": "256",
                               "opcache/8.3/opcache.interned_strings_buffer": "16",
                               "opcache/8.3/opcache.max_accelerated_files": "32531"})

    def test_strings_buffer_default_is_raised_too(self):
        got = {f.id: f.proposed for f in self.audit()["findings"] if tune.applicable(f)}
        self.assertEqual(got["opcache/8.3/opcache.interned_strings_buffer"], "16")
        self.count.return_value = (40000, True)
        got = {f.id: f.proposed for f in self.audit()["findings"] if tune.applicable(f)}
        self.assertEqual(got["opcache/8.3/opcache.interned_strings_buffer"], "32")

    def test_never_lowers_a_value(self):
        self.put("/etc/php/8.3/fpm/conf.d/20-big.ini", "opcache.memory_consumption=1024\nopcache.max_accelerated_files=65407\nopcache.interned_strings_buffer=32\n")
        self.assertEqual([i for i in self.ids() if i.startswith("opcache/")], [])

    def test_a_small_app_needs_nothing(self):
        self.count.return_value = (3000, True)
        self.assertEqual([i for i in self.ids() if i.startswith("opcache/")], [])

    def test_a_current_value_counts_as_its_prime(self):
        self.count.return_value = (7000, True)  # wants 16229; the default 10000 is already 16229 slots
        self.assertEqual([i for i in self.ids() if i.startswith("opcache/")], [])

    def test_unknown_file_count_still_flags_disabled_opcache(self):
        self.count.return_value = (0, True)
        self.put("/etc/php/8.3/fpm/conf.d/20-off.ini", "opcache.enable=0\n")
        f = self.finding("opcache/8.3/opcache.enable")
        self.assertEqual((f.proposed, f.severity), ("1", "warn"))
        self.assertEqual([i for i in self.ids() if i.startswith("opcache/")], ["opcache/8.3/opcache.enable"])

    def test_extension_not_loaded_is_manual(self):
        (self.fs / "etc/php/8.3/fpm/conf.d/10-opcache.ini").unlink()
        f = self.finding("opcache/8.3/extension")
        self.assertEqual(f.severity, "warn")
        self.assertFalse(tune.applicable(f))
        self.assertEqual([i for i in self.ids() if i.startswith("opcache/")], ["opcache/8.3/extension"])

    def test_validate_timestamps_off_is_a_note(self):
        self.count.return_value = (3000, True)
        self.put("/etc/php/8.3/fpm/conf.d/20-prod.ini", "opcache.validate_timestamps=0\n")
        f = self.finding("opcache/8.3/validate_timestamps")
        self.assertEqual(f.severity, "info")
        self.assertFalse(tune.applicable(f))

    def test_a_later_file_that_overrides_our_drop_in_is_reported_not_written(self):
        self.put("/etc/php/8.3/fpm/conf.d/zz-tuning.ini", "opcache.memory_consumption=64\n")
        f = self.finding("opcache/8.3/override/opcache.memory_consumption")
        self.assertIn("zz-tuning.ini", f.current)
        self.assertFalse(tune.applicable(f))
        self.assertNotIn("opcache/8.3/opcache.memory_consumption", self.ids())

    def test_php_ini_value_is_overridable_by_our_drop_in(self):
        self.put("/etc/php/8.3/fpm/php.ini", "opcache.memory_consumption=64\n")  # php.ini loads before conf.d
        self.assertIn("opcache/8.3/opcache.memory_consumption", self.ids())

    def test_edit_check_rejects_nonsense(self):
        chk = self.finding("opcache/8.3/opcache.memory_consumption").check
        self.assertIsNone(chk("512"))
        self.assertTrue(chk("lots") and chk("0") and chk("-5") and chk("99999"))

    def test_render_dropin(self):
        text = tune.render_dropin({"opcache.memory_consumption": "256", "opcache.enable": "1"})
        self.assertTrue(text.startswith(";"))
        self.assertEqual(text.splitlines()[1:], ["opcache.enable = 1", "opcache.memory_consumption = 256"])

    def test_verify(self):
        self.put(DROPIN, "opcache.memory_consumption = 256\n")
        want = {"opcache.memory_consumption": "256"}
        self.assertIsNone(tune.verify_opcache(self.host, "8.3", want))
        self.put("/etc/php/8.3/fpm/conf.d/zz-x.ini", "opcache.memory_consumption=64\n")
        self.assertIn("zz-x.ini", tune.verify_opcache(self.host, "8.3", want))


class TestFpm(Fs):
    def setUp(self):
        super().setUp()
        self.mem(4096)
        self.neighbours()

    def pool(self, children, **extra):
        conf = "pm = dynamic\n" + f"pm.max_children = {children}\n" + "".join(f"{k} = {v}\n" for k, v in extra.items())
        self.php(pool=conf, opcache=False)
        self.workers("8.3", 4, 60)

    def test_over_committed_pool_gets_a_lower_value(self):
        self.pool(60, **{"pm.max_requests": 500})
        f = self.finding("fpm/8.3/www/max_children")
        self.assertEqual(f.severity, "warn")
        self.assertIn("pm.max_children = 32", f.proposed)  # (4096 - 512 - 1.2 * 930) / 75
        self.assertIn("pm.max_children = 60", f.current)
        self.assertFalse(tune.applicable(f))
        self.assertIn("/etc/php/8.3/fpm/pool.d/www.conf", f.risk)

    def test_a_raise_needs_evidence_from_the_log(self):
        self.pool(5, **{"pm.max_requests": 500})
        self.assertNotIn("fpm/8.3/www/max_children", self.ids())      # RAM allows more, but nothing is saturated
        self.put("/var/log/php8.3-fpm.log", FPM_LOG_NOW)
        f = self.finding("fpm/8.3/www/max_children")
        self.assertEqual(f.severity, "info")
        self.assertIn("pm.max_children = 10", f.proposed)
        self.assertIn("pm.start_servers = 3", f.proposed)
        self.assertIn("pm.max_spare_servers = 5", f.proposed)
        self.assertIn("1 time", f.current)

    def test_few_workers_use_an_assumed_size(self):
        self.php(pool="pm = dynamic\npm.max_children = 5\npm.max_requests = 500\n", opcache=False)
        self.workers("8.3", 1, 60)
        self.put("/var/log/php8.3-fpm.log", FPM_LOG_NOW)
        self.assertIn("assumed", self.finding("fpm/8.3/www/max_children").risk)

    def test_saturated_without_headroom_says_so(self):
        self.mem(1024)
        self.pool(2, **{"pm.max_requests": 500})
        self.put("/var/log/php8.3-fpm.log", FPM_LOG_NOW)
        f = self.finding("fpm/8.3/www/max_children")
        self.assertEqual(f.severity, "info")
        self.assertIn("no RAM", f.title)

    def test_inside_the_band_nothing_is_said(self):
        self.pool(34, **{"pm.max_requests": 500})
        self.assertEqual([i for i in self.ids() if i.startswith("fpm/")], [])

    def test_max_requests_unset_is_a_note(self):
        self.pool(34)
        f = self.finding("fpm/8.3/www/max_requests")
        self.assertIn("500", f.proposed)
        self.assertFalse(tune.applicable(f))

    def test_static_pools_get_no_spare_servers(self):
        self.php(pool="pm = static\npm.max_children = 5\npm.max_requests = 500\n", opcache=False)
        self.workers("8.3", 4, 60)
        self.put("/var/log/php8.3-fpm.log", FPM_LOG_NOW)
        self.assertNotIn("spare", self.finding("fpm/8.3/www/max_children").proposed)

    def test_pools_share_the_budget_by_their_current_size(self):
        self.pool(60)
        self.put("/etc/php/8.3/fpm/pool.d/api.conf", "[api]\npm = dynamic\npm.max_children = 60\npm.max_requests = 500\n")
        self.workers("8.3", 4, 60, name="api")
        self.put("/etc/php/8.3/fpm/pool.d/www.conf", "[www]\npm = dynamic\npm.max_children = 60\npm.max_requests = 500\n")
        # 2468 MB split in two = 1234 MB each: 16 children of 75 MB. FPM workers are nobody's neighbours.
        self.assertIn("pm.max_children = 16", self.finding("fpm/8.3/www/max_children").proposed)
        self.assertIn("pm.max_children = 16", self.finding("fpm/8.3/api/max_children").proposed)

    def test_a_stopped_php_version_is_skipped(self):
        self.pool(60)
        self.put("/etc/php/8.1/fpm/pool.d/www.conf", "[www]\npm = dynamic\npm.max_children = 500\n")
        self.assertEqual([i for i in self.ids() if "/8.1/" in i], [])


class TestOtherServices(Fs):
    def setUp(self):
        super().setUp()
        self.mem(2048)

    def test_php_ini(self):
        self.php(ini="memory_limit = -1\nupload_max_filesize = 64M\npost_max_size = 8M\n", opcache=False,
                 pool="pm = static\npm.max_children = 5\npm.max_requests = 500\n")
        self.assertEqual(self.finding("php/8.3/memory_limit").proposed, "memory_limit = 256M")
        self.assertEqual(self.finding("php/8.3/post_max_size").proposed, "post_max_size = 66M")
        self.php(ini="memory_limit = 256M\nupload_max_filesize = 2M\npost_max_size = 8M\n", opcache=False,
                 pool="pm = static\npm.max_children = 5\npm.max_requests = 500\n")
        self.assertEqual([i for i in self.ids() if i.startswith("php/")], [])

    def nginx(self, conf, limits="Max open files            1024                 524288               files\n", log=""):
        self.runner.units.append("nginx.service")
        self.put("/etc/nginx/nginx.conf", conf)
        self.proc("nginx", "nginx: master process /usr/sbin/nginx", limits=limits)
        if log:
            self.put("/var/log/nginx/error.log", log)

    def test_nginx_problems(self):
        self.php(ini="upload_max_filesize = 64M\npost_max_size = 64M\n", opcache=False,
                 pool="pm = static\npm.max_children = 5\npm.max_requests = 500\n")
        self.nginx("worker_processes 1;\nevents { worker_connections 4096; }\nhttp { gzip off; client_max_body_size 2m; }\n",
                   log="2026/11/04 10:00:00 [alert] 1#1: 4096 worker_connections are not enough\n")
        got = {i: self.finding(i) for i in self.ids() if i.startswith("nginx/")}
        self.assertEqual(sorted(got), ["nginx/body_size", "nginx/gzip", "nginx/worker_connections", "nginx/worker_fds", "nginx/worker_processes"])
        self.assertEqual(got["nginx/worker_processes"].proposed, "worker_processes auto;")
        self.assertIn("worker_rlimit_nofile 16384;", got["nginx/worker_fds"].proposed)  # 2 x the proposed 8192 connections
        self.assertIn("worker_connections 8192;", got["nginx/worker_connections"].proposed)
        self.assertIn("client_max_body_size 64M;", got["nginx/body_size"].proposed)
        self.assertTrue(all(not tune.applicable(f) for f in got.values()))

    def test_nginx_that_is_fine(self):
        self.nginx("worker_processes auto;\nevents { worker_connections 1024; }\n"
                   "http { gzip on; gzip_types text/css application/json; client_max_body_size 128m; }\n",
                   limits="Max open files            524288               524288               files\n")
        self.assertEqual([i for i in self.ids() if i.startswith("nginx/")], [])

    def test_nginx_rlimit_directive_counts(self):
        self.nginx("worker_processes auto;\nworker_rlimit_nofile 20000;\nevents { worker_connections 4096; }\nhttp { gzip on; gzip_types text/css; }\n")
        self.assertEqual([i for i in self.ids() if i.startswith("nginx/")], [])

    def test_nginx_not_running_is_skipped_quietly(self):
        self.put("/etc/nginx/nginx.conf", "worker_processes 1;\n")
        self.assertEqual(self.ids(), [])

    def apache(self, mpm, conf, procs=3, mb=45):
        self.runner.units.append("apache2.service")
        self.put(f"/etc/apache2/mods-enabled/mpm_{mpm}.load", "")
        self.put(f"/etc/apache2/mods-enabled/mpm_{mpm}.conf", conf)
        for _ in range(procs):
            self.proc("apache2", "/usr/sbin/apache2 -k start", mb=mb)

    def test_apache_prefork_worst_case_exceeds_ram(self):
        self.apache("prefork", "MaxRequestWorkers 150\n")
        f = self.finding("apache/max_request_workers")
        self.assertEqual(f.proposed, "MaxRequestWorkers 27")  # (2048 - 512) / (45 * 1.25)
        self.assertFalse(tune.applicable(f))

    def test_apache_event_counts_processes_not_threads(self):
        self.apache("event", "MaxRequestWorkers 150\nThreadsPerChild 25\n")  # 6 processes x 56 MB: fits
        self.assertEqual(self.ids(), [])
        self.apache("event", "MaxRequestWorkers 3000\nThreadsPerChild 25\n")  # 120 processes: does not
        self.assertEqual(self.finding("apache/max_request_workers").proposed, "MaxRequestWorkers 675")  # 27 processes x 25

    def test_redis_points_to_its_own_module(self):
        self.runner.units.append("redis-server.service")
        self.proc("redis-server", mb=40)
        f = self.finding("redis/tuning")
        self.assertIn(f"{self.root}/current/softsynk-server configure redis", f.proposed)
        self.assertEqual(f.severity, "info")

    def test_meilisearch_without_a_memory_cap(self):
        self.runner.units.append("meilisearch.service")
        pid = self.proc("meilisearch", "/usr/bin/meilisearch --db-path /var/lib/meili", mb=300)
        self.assertEqual(self.finding("meilisearch/indexing_memory").proposed, 'max_indexing_memory = "512 MiB"')
        self.put("/etc/meilisearch.toml", "# max_indexing_memory = \"1 GiB\"\n")
        self.assertIsNotNone(self.finding("meilisearch/indexing_memory"))   # a commented line is no cap
        self.put("/etc/meilisearch.toml", 'max_indexing_memory = "1 GiB"\n')
        self.assertNotIn("meilisearch/indexing_memory", self.ids())
        (self.fs / "etc/meilisearch.toml").unlink()
        self.put(f"/proc/{pid}/environ", "A=b\0MEILI_MAX_INDEXING_MEMORY=2GiB\0")
        self.assertNotIn("meilisearch/indexing_memory", self.ids())

    def test_databases_only_show_in_the_overview(self):
        self.proc("mysqld", mb=500)
        self.runner.units.append("mysql.service")
        res = self.audit()
        self.assertEqual(res["findings"], [])
        top = res["services"][0]
        self.assertEqual((top["name"], top["pss"], top["percent"]), ("mysql", 500 * MB, 24.4))


class TestAuditOutput(Fs):
    def setUp(self):
        super().setUp()
        self.mem(2048)
        self.php(ini="memory_limit = -1\n", opcache=False, pool="pm = static\npm.max_children = 5\npm.max_requests = 500\n")
        self.proc("mysqld", mb=500)

    def test_text(self):
        rc, out, _ = self.call("audit")
        self.assertEqual(rc, 0)
        self.assertIn("2.0 GiB", out)
        self.assertIn("mysql", out)
        self.assertIn("memory_limit", out)
        self.assertIn("report only", out)

    def test_audit_changes_nothing(self):
        before = sorted(str(p) for p in self.tmp.rglob("*") if "proc" not in p.parts)
        self.call("audit")
        self.assertEqual(sorted(str(p) for p in self.tmp.rglob("*") if "proc" not in p.parts), before)
        self.assertFalse(self.runner.ran("systemctl", "reload"))

    def test_unreadable_meminfo_is_a_failure(self):
        (self.fs / "proc/meminfo").unlink()
        rc, _, err = self.call("audit")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("meminfo", err)


class TestApply(Fs):
    def setUp(self):
        super().setUp()
        self.mem(4096)
        self.php()
        count = mock.patch.object(tune, "count_php_files", return_value=(20000, True))
        count.start()
        self.addCleanup(count.stop)

    def test_accept_defaults_writes_the_drop_in_and_reloads(self):
        rc, out, err = self.call("--accept-defaults")
        self.assertEqual(rc, 0, err)
        text = rd(self.fs / DROPIN.lstrip("/"))
        self.assertIn("opcache.memory_consumption = 256", text)
        self.assertIn("opcache.max_accelerated_files = 32531", text)
        self.assertIn("Managed by synk-tune", text)
        self.assertEqual(self.runner.ran("php-fpm8.3", "-t"), [["php-fpm8.3", "-t"]] * 2)  # before and after the write
        self.assertEqual(self.runner.ran("systemctl", "reload", "php8.3-fpm"), [["systemctl", "reload", "php8.3-fpm"]])
        stamp = change.latest(self.ctx)
        self.assertTrue(stamp)
        self.assertIn(f"{self.root}/current/deployment/optimizer/synk-tune rollback --stamp {stamp}", err + out)
        self.assertIn("+opcache.memory_consumption = 256", out)  # the preview

    def test_second_run_has_nothing_to_do(self):
        self.call("--accept-defaults")
        self.runner.calls.clear()
        rc, out, err = self.call("--accept-defaults")
        self.assertEqual(rc, 0)
        self.assertFalse(self.runner.ran("systemctl", "reload"))
        self.assertIn("Nothing", err)
        self.assertEqual(len(os.listdir(self.root / "data/optimizer/backups")), 1)

    def test_dry_run_changes_nothing(self):
        rc, out, _ = self.call("--accept-defaults", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("+opcache.memory_consumption = 256", out)
        self.assertFalse((self.fs / DROPIN.lstrip("/")).exists())
        self.assertFalse(self.runner.ran("systemctl", "reload"))

    def test_env_dry_run_is_honoured(self):
        os.environ["SOFTSYNK_DRY_RUN"] = "1"
        rc, _, _ = self.call("--accept-defaults")
        self.assertEqual(rc, 0)
        self.assertFalse((self.fs / DROPIN.lstrip("/")).exists())

    def test_failed_validation_puts_everything_back(self):
        self.runner.rcs["php-fpm8.3"] = [0, 1]  # the current config is fine, the new one is not
        rc, _, err = self.call("--accept-defaults")
        self.assertEqual(rc, core.FAILED)
        self.assertFalse((self.fs / DROPIN.lstrip("/")).exists())
        self.assertIn("php-fpm8.3 -t", err)
        self.assertFalse(self.runner.ran("systemctl", "reload"))

    def test_failed_reload_restores_and_reloads_again(self):
        self.runner.rcs["systemctl"] = [1, 0]
        rc, _, err = self.call("--accept-defaults")
        self.assertEqual(rc, core.FAILED)
        self.assertFalse((self.fs / DROPIN.lstrip("/")).exists())
        self.assertEqual(len(self.runner.ran("systemctl", "reload", "php8.3-fpm")), 2)

    def test_choices_are_honoured(self):
        os.environ["SOFTSYNK_ANSWER_FINDING_OPCACHE_8_3_OPCACHE_MAX_ACCELERATED_FILES"] = "skip"
        os.environ["SOFTSYNK_ANSWER_FINDING_OPCACHE_8_3_OPCACHE_MEMORY_CONSUMPTION"] = "edit"
        os.environ["SOFTSYNK_ANSWER_VALUE_OPCACHE_8_3_OPCACHE_MEMORY_CONSUMPTION"] = "384"
        rc, _, err = self.call("--accept-defaults")
        self.assertEqual(rc, 0, err)
        text = rd(self.fs / DROPIN.lstrip("/"))
        self.assertIn("opcache.memory_consumption = 384", text)
        self.assertNotIn("max_accelerated_files", text)

    def test_an_invalid_edit_is_refused(self):
        os.environ["SOFTSYNK_ANSWER_FINDING_OPCACHE_8_3_OPCACHE_MEMORY_CONSUMPTION"] = "edit"
        os.environ["SOFTSYNK_ANSWER_VALUE_OPCACHE_8_3_OPCACHE_MEMORY_CONSUMPTION"] = "lots"
        rc, _, err = self.call("--accept-defaults")
        self.assertEqual(rc, core.USAGE)
        self.assertFalse((self.fs / DROPIN.lstrip("/")).exists())

    def test_existing_drop_in_keys_are_kept(self):
        self.put(DROPIN, "opcache.revalidate_freq = 30\nopcache.memory_consumption = 128\n")
        self.call("--accept-defaults")
        text = rd(self.fs / DROPIN.lstrip("/"))
        self.assertIn("opcache.revalidate_freq = 30", text)
        self.assertIn("opcache.memory_consumption = 256", text)

    def test_each_php_version_gets_its_own_drop_in_and_reload(self):
        self.php("8.4")
        self.call("--accept-defaults")
        for v in ("8.3", "8.4"):
            self.assertTrue((self.fs / f"etc/php/{v}/fpm/conf.d/99-softsynk-opcache.ini").exists())
            self.assertTrue(self.runner.ran("systemctl", "reload", f"php{v}-fpm"))

    def test_one_version_failing_does_not_stop_the_other(self):
        self.php("8.4")
        self.runner.rcs["php-fpm8.4"] = [0, 1]
        rc, _, _ = self.call("--accept-defaults")
        self.assertEqual(rc, core.FAILED)
        self.assertTrue((self.fs / DROPIN.lstrip("/")).exists())
        self.assertFalse((self.fs / "etc/php/8.4/fpm/conf.d/99-softsynk-opcache.ini").exists())

    def test_without_accept_defaults_a_script_cannot_proceed(self):
        rc, _, err = self.call()
        self.assertEqual(rc, core.USAGE)
        self.assertFalse((self.fs / DROPIN.lstrip("/")).exists())

    def test_nothing_applicable_means_nothing_to_do(self):
        self.put("/etc/php/8.3/fpm/conf.d/20-big.ini", "opcache.memory_consumption=1024\nopcache.max_accelerated_files=65407\nopcache.interned_strings_buffer=32\n")
        rc, _, err = self.call()
        self.assertEqual(rc, 0)
        self.assertIn("Nothing", err)

    def test_rollback_restores_and_reloads(self):
        self.put(DROPIN, "opcache.memory_consumption = 128\n")
        self.call("--accept-defaults")
        self.assertIn("256", rd(self.fs / DROPIN.lstrip("/")))
        self.runner.calls.clear()
        rc, _, err = self.call("rollback")
        self.assertEqual(rc, 0, err)
        self.assertEqual(rd(self.fs / DROPIN.lstrip("/")), "opcache.memory_consumption = 128\n")
        self.assertEqual(self.runner.ran("systemctl", "reload", "php8.3-fpm"), [["systemctl", "reload", "php8.3-fpm"]])

    def test_rollback_of_a_new_drop_in_removes_it_and_names_the_stamp(self):
        self.call("--accept-defaults")
        stamp = change.latest(self.ctx)
        rc, _, err = self.call("rollback", "--stamp", stamp)
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.fs / DROPIN.lstrip("/")).exists())
        self.assertIn(stamp, err)

    def test_rollback_without_backups_or_with_a_bad_stamp(self):
        rc, _, err = self.call("rollback")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("No backups", err)
        rc, _, _ = self.call("rollback", "--stamp", "nonsense")
        self.assertEqual(rc, core.FAILED)

    def test_applying_needs_root_on_a_real_system(self):
        with mock.patch.object(os, "geteuid", return_value=1000):
            with self.assertRaises(core.Fail):
                tune.need_root(tune.Host("/"))
            tune.need_root(self.host)  # a temp tree is not the system

    def test_verify_failure_rolls_back(self):
        with mock.patch.object(tune, "verify_opcache", return_value="opcache.memory_consumption is 64"):
            rc, _, err = self.call("--accept-defaults")
        self.assertEqual(rc, core.FAILED)
        self.assertFalse((self.fs / DROPIN.lstrip("/")).exists())
        self.assertIn("verification failed", err)


class TestStatus(Fs):
    def status(self):
        return tune.build_status(self.ctx, self.host)

    def test_not_installed_is_quiet(self):
        s = self.status()
        self.assertEqual((s["module"], s["install_state"], s["health"]), ("optimizer", "not_installed", "unknown"))
        self.assertEqual(s["warnings"], [])

    def test_installed_and_healthy(self):
        self.php()
        self.put(DROPIN, "opcache.memory_consumption = 256\n")
        s = self.status()
        self.assertEqual((s["install_state"], s["health"]), ("installed", "healthy"))
        self.assertEqual(s["details"]["managed_files"], [DROPIN])

    def test_saturation_in_the_last_day_degrades(self):
        self.php()
        self.put(DROPIN, "opcache.memory_consumption = 256\n")
        self.put("/var/log/php8.3-fpm.log", FPM_LOG_NOW)
        s = self.status()
        self.assertEqual(s["health"], "degraded")
        self.assertEqual(s["details"]["saturated"], {"8.3/www": 1})
        self.assertEqual(s["actions"], ["synk-tune"])
        self.assertTrue(any("pm.max_children" in w for w in s["warnings"]))

    def test_older_saturation_does_not(self):
        self.php()
        self.put(DROPIN, "opcache.memory_consumption = 256\n")
        self.put("/var/log/php8.3-fpm.log", FPM_LOG_NOW.replace("04-Nov", "02-Nov"))
        self.assertEqual(self.status()["health"], "healthy")

    def test_saturation_degrades_even_before_anything_was_applied(self):
        self.php()
        self.put("/var/log/php8.3-fpm.log", FPM_LOG_NOW)
        s = self.status()
        self.assertEqual((s["install_state"], s["health"]), ("not_installed", "degraded"))

    def test_status_json_flag_exits_zero_and_prints_the_document(self):
        self.php()
        self.put("/var/log/php8.3-fpm.log", FPM_LOG_NOW)
        rc, out, _ = self.call("--status-json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["health"], "degraded")


class TestSmoke(unittest.TestCase):
    def test_real_script_status_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            r = subprocess.run([sys.executable, SCRIPT, "--status-json", "--root", root], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual((doc["schema"], doc["module"]), (1, "optimizer"))
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
