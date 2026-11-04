"""synk-clean: safety of the deletion walk, rule matching per category, estimates, renders, status, CLI flows.
Temp trees and scripted commands only; nothing here touches the real machine."""
import json
import os
import unittest
import unittest.mock as mock
from pathlib import Path

from cleantest import Base, clean, core

G = 1024 ** 3
JOURNAL_1G = core.Result(0, "Archived and active journals take up 1.0G in the file system.\n")


class Walk(Base):
    """sweep(): the only code that deletes files."""

    def sweep(self, root=None, delete=True, match=clean.rotated_match, days=30, protected=None):
        prot = self.host().protected if protected is None else protected
        return clean.sweep(str(root or self.var_log), match, self.cutoff(days), prot, delete)

    def test_deletes_only_old_matching_regular_files(self):
        L = self.var_log
        old = self.mk(L / "auth.log.1", 40, 100)
        new = self.mk(L / "auth.log.2", 5)
        live = self.mk(L / "auth.log", 90)
        other = self.mk(L / "notes.txt", 90)
        r = self.sweep()
        self.assertEqual((r["files"], r["bytes"], r["errors"]), (1, 100, []))
        self.assertFalse(old.exists())
        self.assertTrue(all(p.exists() for p in (new, live, other)))

    def test_age_is_strictly_older_than_the_rule(self):
        young = self.mk(self.var_log / "a.log.1", 29)
        old = self.mk(self.var_log / "b.log.1", 31)
        self.sweep()
        self.assertTrue(young.exists())
        self.assertFalse(old.exists())

    def test_estimate_equals_what_is_deleted(self):
        for i, n in enumerate((10, 20, 30)):
            self.mk(self.var_log / f"d{i}" / "x.log.1", 40, n)
        est = self.sweep(delete=False)
        self.assertEqual((est["files"], est["bytes"]), (3, 60))
        self.assertTrue(all(p.exists() for p in self.var_log.rglob("x.log.1")))  # estimating deletes nothing
        done = self.sweep()
        self.assertEqual((done["files"], done["bytes"]), (3, 60))
        self.assertEqual(list(self.var_log.rglob("x.log.1")), [])

    def test_symlink_to_a_file_is_never_deleted_or_followed(self):
        outside = self.mk(self.fs / "outside" / "precious.log.1", 90)
        link = self.var_log / "x.log.1"
        os.symlink(outside, link)
        os.utime(link, (self.cutoff(90),) * 2, follow_symlinks=False)
        r = self.sweep()
        self.assertEqual(r["files"], 0)
        self.assertTrue(link.is_symlink() and outside.exists())

    def test_symlinked_directory_is_not_entered(self):
        outside = self.mk(self.fs / "outside" / "precious.log.1", 90)
        os.symlink(outside.parent, self.var_log / "linked")
        self.sweep()
        self.assertTrue(outside.exists())

    def test_symlinked_root_is_skipped(self):
        outside = self.mk(self.fs / "outside" / "precious.log.1", 90)
        os.symlink(outside.parent, self.fs / "rootlink")
        self.assertEqual(self.sweep(self.fs / "rootlink")["files"], 0)
        self.assertTrue(outside.exists())

    def test_protected_directory_is_pruned_and_the_rest_still_cleaned(self):
        kept = self.mk(self.var_log / "mysql" / "error.log.1", 90)
        also = self.mk(self.var_log / "journal" / "x.log.1", 90)
        gone = self.mk(self.var_log / "nginx" / "access.log.1", 90)
        self.sweep()
        self.assertTrue(kept.exists() and also.exists())
        self.assertFalse(gone.exists())

    def test_protected_root_is_refused_with_an_error(self):
        f = self.mk(self.var_log / "mysql" / "error.log.1", 90)
        r = self.sweep(self.var_log / "mysql")
        self.assertEqual(r["files"], 0)
        self.assertTrue(r["errors"] and "protected" in r["errors"][0])
        self.assertTrue(f.exists())

    def test_protected_file_is_never_deleted(self):
        f = self.mk(self.var_log / "keep.log.1", 90)
        self.sweep(protected=(str(f),))
        self.assertTrue(f.exists())

    def test_directory_swapped_for_a_symlink_mid_walk_never_deletes_outside(self):
        L = self.var_log
        self.mk(L / "sub" / "victim.log.1", 40)
        outside = self.mk(self.fs / "outside" / "victim.log.1", 40)
        real, swapped = os.lstat, []

        def lstat(path, *a, **kw):
            if path == "victim.log.1" and kw.get("dir_fd") is not None and not swapped:
                swapped.append(1)
                os.rename(L / "sub", L / "sub.moved")
                os.symlink(outside.parent, L / "sub")
            return real(path, *a, **kw)

        with mock.patch.object(os, "lstat", lstat):
            self.sweep()
        self.assertTrue(swapped)
        self.assertTrue(outside.exists())  # the unlink went through the directory it had opened, not the new name
        self.assertFalse((L / "sub.moved" / "victim.log.1").exists())

    def test_file_replaced_or_touched_between_stat_and_unlink_is_skipped(self):
        outside = self.mk(self.fs / "outside" / "precious", 90)
        f = self.var_log / "x.log.1"

        def to_symlink():
            os.unlink(f)
            os.symlink(outside, f)

        def touch():
            os.utime(f, (self.cutoff(0),) * 2)

        def other_file():
            n = self.mk(self.var_log / "new.tmp", 90)
            os.rename(n, f)

        for name, action in (("symlink", to_symlink), ("touched", touch), ("other file", other_file)):
            with self.subTest(name):
                self.mk(f, 90)
                real, calls = os.lstat, []

                def lstat(path, *a, **kw):
                    if path == "x.log.1" and kw.get("dir_fd") is not None:
                        calls.append(1)
                        if len(calls) == 2:
                            action()
                    return real(path, *a, **kw)

                with mock.patch.object(os, "lstat", lstat):
                    r = self.sweep()
                self.assertEqual((r["files"], r["skipped"]), (0, 1))
                self.assertTrue(os.path.lexists(f))
                self.assertTrue(outside.exists())
                os.unlink(f)

    def test_other_filesystem_is_skipped(self):
        L = self.var_log
        a = self.mk(L / "a.log.1", 40)
        b = self.mk(L / "mnt" / "b.log.1", 40)
        c = self.mk(L / "c.log.1", 40)
        real = os.lstat

        def lstat(path, *a, **kw):
            st = real(path, *a, **kw)
            if kw.get("dir_fd") is not None and path in ("mnt", "c.log.1"):
                f = list(st)
                f[2] += 1  # st_dev
                return os.stat_result(f)
            return st

        with mock.patch.object(os, "lstat", lstat):
            r = self.sweep()
        self.assertEqual(r["files"], 1)
        self.assertFalse(a.exists())
        self.assertTrue(b.exists() and c.exists())

    def test_no_age_limit_when_cutoff_is_none(self):
        f = self.mk(self.fs / "cache" / "a", 0)
        r = clean.sweep(str(self.fs / "cache"), lambda n: True, None, (), True)
        self.assertEqual(r["files"], 1)
        self.assertFalse(f.exists())


class Rules(Base):
    def test_rotated_globs(self):
        yes = ["syslog.2.gz", "auth.log.1", "kern.log.2.gz", "Xorg.0.log.old", "dpkg.log.1", "access.log.9", "history.log.3.gz"]
        no = ["syslog", "syslog.1", "auth.log", "wtmp", "btmp.1", "foo.gz", "app.log.10", "notes.txt", "x.journal", "a.log.1.bak"]
        self.assertEqual([n for n in yes if not clean.rotated_match(n)], [])
        self.assertEqual([n for n in no if clean.rotated_match(n)], [])

    def test_laravel_names(self):
        yes = ["laravel-2026-09-01.log", "laravel-2026-09-01.log.gz", "laravel.log.2.gz"]
        no = ["laravel.log", "laravel.log.1", "worker.log", "notes.txt", "laravel-2026-09-01.log.bak"]
        self.assertEqual([n for n in yes if not clean.laravel_match(n)], [])
        self.assertEqual([n for n in no if clean.laravel_match(n)], [])

    def test_parse_size(self):
        self.assertEqual([clean.parse_size(s) for s in ("500M", "2G", "10K", "7", "1T")], [500 * 1024 ** 2, 2 * G, 10240, 7, 1024 ** 4])

    def test_default_protected_list(self):
        p = clean.default_host(self.ctx).protected
        for path in ("/etc", "/boot", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/var/lib/mysql", "/var/lib/postgresql", "/var/lib/redis",
                     "/var/lib/meilisearch", "/var/log/mysql", "/var/log/journal", str(self.root / "data"), str(self.root / "secrets"),
                     str(self.root / "state"), str(self.root / "etc")):
            self.assertIn(path, p)
        self.assertTrue(clean.is_protected("/etc/ssh/sshd_config", p))
        self.assertTrue(clean.is_protected("/var/lib/mysql", p))
        self.assertFalse(clean.is_protected("/etcetera/x", p))
        self.assertFalse(clean.is_protected("/var/log/nginx", p))


class Config(Base):
    def load(self, **raw):
        core.write_json(self.ctx.config_path, raw)
        return clean.load_cfg(self.ctx)

    def test_defaults_when_no_file(self):
        self.assertEqual(clean.load_cfg(self.ctx), clean.DEFAULTS)

    def test_override_is_merged(self):
        c = self.load(keep_days=45, journal_max="1G")
        self.assertEqual((c["keep_days"], c["journal_max"], c["crash_keep_days"]), (45, "1G", 7))

    def test_bad_values_are_refused_with_exit_64(self):
        bad = [{"keep_days": 27}, {"keep_days": "30"}, {"crash_keep_days": 0}, {"journal_max": "0M"}, {"journal_max": "5x"},
               {"laravel_rotate_days": 0}, {"large_file_mb": 0}, {"app_roots": ["home/*"]}, {"app_roots": [1]}, {"nope": 1}]
        for raw in bad:
            with self.subTest(raw), self.assertRaises(core.Fail) as cm:
                self.load(**raw)
            self.assertEqual(cm.exception.code, 64)


class Plans(Base):
    def plans(self, host=None, **cfg):
        return {p.id: p for p in clean.build_plans(host or self.host(), self.cfg(**cfg), self.ctx)}

    def test_rotated_follows_keep_days(self):
        self.mk(self.var_log / "syslog.2.gz", 40, 100)
        self.mk(self.var_log / "nginx" / "access.log.3", 90, 200)
        self.mk(self.var_log / "auth.log.1", 10, 50)
        p = self.plans()["rotated"]
        self.assertEqual((p.files, p.bytes), (2, 300))
        self.assertEqual(self.plans(keep_days=100)["rotated"].files, 0)

    def test_laravel_apps(self):
        a = self.fs / "home/forge/a.com"
        self.mk(a / "artisan")
        logs = a / "storage/logs"
        old, gz = self.mk(logs / "laravel-2026-09-01.log", 60, 100), self.mk(logs / "laravel-2026-08-01.log.gz", 90, 50)
        live, young = self.mk(logs / "laravel.log", 90), self.mk(logs / "laravel-2026-10-30.log", 5)
        b = self.fs / "home/forge/b.com"  # storage lives outside the app (shared dir): never walked
        self.mk(b / "artisan")
        shared = self.mk(self.fs / "shared/storage/logs/laravel-2026-01-01.log", 90)
        os.symlink(shared.parent.parent, b / "storage")
        plain = self.mk(self.fs / "home/forge/plain/storage/logs/laravel-2026-01-01.log", 90)  # no artisan: not an app
        p = self.plans()["laravel"]
        self.assertEqual((p.files, p.bytes), (2, 150))
        self.assertIn("b.com", p.note)
        p.do()
        self.assertFalse(old.exists() or gz.exists())
        self.assertTrue(all(f.exists() for f in (live, young, shared, plain)))

    def test_crash_uses_its_own_age(self):
        old = self.mk(self.fs / "var/crash/x.crash", 10)
        young = self.mk(self.fs / "var/lib/systemd/coredump/core.1", 3)
        p = self.plans()["crash"]
        self.assertEqual(p.files, 1)
        p.do()
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())

    def test_apt_counts_only_cached_packages_and_runs_apt_get_clean(self):
        d = self.fs / "var/cache/apt/archives"
        self.mk(d / "a.deb", 0, 1000), self.mk(d / "b.deb", 0, 2000), self.mk(d / "lock", 0, 5), self.mk(d / "partial/c.deb", 0, 9999)
        p = self.plans()["apt"]
        self.assertEqual((p.files, p.bytes), (2, 3000))
        self.assertEqual(p.do()["errors"], [])
        self.assertEqual(self.runner.calls[-1], ["apt-get", "clean"])

    def test_apt_failure_is_reported(self):
        d = self.fs / "var/cache/apt/archives"
        self.mk(d / "a.deb", 0, 1000)
        self.runner.answers["apt-get clean"] = core.Result(100, "", "E: Could not get lock")
        self.assertIn("lock", self.plans()["apt"].do()["errors"][0])

    def test_journal_estimate_is_usage_above_the_cap(self):
        self.runner.answers["journalctl --disk-usage"] = JOURNAL_1G
        p = self.plans()["journal"]
        self.assertEqual(p.bytes, G - 500 * 1024 ** 2)
        p.do()
        self.assertTrue(self.runner.ran("journalctl --vacuum-size=500M"))

    def test_journal_below_cap_or_missing_is_zero(self):
        self.runner.answers["journalctl --disk-usage"] = core.Result(0, "Archived and active journals take up 120.0M in the file system.\n")
        self.assertEqual(self.plans()["journal"].bytes, 0)
        self.runner.answers["journalctl --disk-usage"] = core.Result(127, "", "journalctl: No such file or directory")
        p = self.plans()["journal"]
        self.assertEqual(p.bytes, 0)
        self.assertIn("journalctl", p.note)

    def test_caches_are_off_by_default_and_ignore_age(self):
        f = self.mk(self.fs / "home/forge/.composer/cache/files/a.zip", 0, 100)
        n = self.mk(self.fs / "root/.npm/_cacache/content/b", 0, 50)
        plans = self.plans()
        self.assertTrue(all(plans[i].default for i in ("apt", "journal", "rotated", "laravel", "crash")))
        p = plans["caches"]
        self.assertEqual((p.default, p.files, p.bytes), (False, 2, 150))
        p.do()
        self.assertFalse(f.exists() or n.exists())

    def test_symlinked_cache_dir_is_not_followed(self):
        outside = self.mk(self.fs / "outside/a", 0)
        (self.fs / "home/forge/.composer").mkdir(parents=True)
        os.symlink(outside.parent, self.fs / "home/forge/.composer/cache")
        self.assertEqual(self.plans()["caches"].files, 0)
        self.assertTrue(outside.exists())

    def test_report_only_items(self):
        rel = self.root / "releases"
        for n in ("r1", "r2", "r3"):
            self.mk(rel / n / "f", 0, 4096)
        os.symlink(rel / "r3", self.root / "current")
        os.symlink(rel / "r2", self.root / "previous")
        self.mk(self.root / "data/backup/backups/20250101-000000/manifest.json", 0)
        self.mk(self.root / "data/backup/backups/20261101-000000/manifest.json", 0)
        self.runner.answers["apt-get -s autoremove"] = core.Result(0, "Remv linux-image-6.8.0-1 [6.8.0]\nRemv foo [1]\nInst bar\n")
        host = self.host(which=lambda n: "/usr/bin/" + n if n == "docker" else None)
        r = clean.report_only(host, self.cfg(), self.ctx)
        self.assertEqual([x["name"] for x in r["old_releases"]], ["r1"])
        self.assertEqual(r["autoremove"], 2)
        self.assertEqual((r["docker"], r["snap"]), (True, False))
        self.assertEqual(r["old_backups"], 1)


class Renders(Base):
    def test_journald(self):
        t = clean.render_journald("500M")
        self.assertIn("[Journal]\nSystemMaxUse=500M\n", t)
        self.assertTrue(t.startswith("# Managed by softsynk"))

    def test_logrotate_stanza_per_app_and_unsafe_paths_skipped(self):
        apps = [clean.App("/home/forge/a.com", "/home/forge/a.com/storage/logs", "forge", "forge"),
                clean.App("/home/x/my app", "/home/x/my app/storage/logs", "x", "x"),
                clean.App("/home/y/b", "/home/y/b/storage/logs", "y;rm", "y")]
        t = clean.render_logrotate(apps, 14)
        self.assertIn("/home/forge/a.com/storage/logs/laravel.log {\n    su forge forge\n    daily\n    rotate 14\n    compress\n"
                      "    delaycompress\n    missingok\n    notifempty\n    copytruncate\n}\n", t)
        self.assertNotIn("my app", t)
        self.assertNotIn("rm", t)
        self.assertEqual(t.count("{"), 1)


class Cli(Base):
    def populate(self):
        L = self.var_log
        self.paths = dict(
            gone=[self.mk(L / "syslog.2.gz", 40, 100), self.mk(L / "nginx/access.log.3", 90, 200),
                  self.mk(self.fs / "home/forge/a.com/storage/logs/laravel-2026-09-01.log", 60, 100),
                  self.mk(self.fs / "var/crash/x.crash", 10)],
            kept=[self.mk(L / "syslog", 0), self.mk(L / "syslog.1", 90), self.mk(L / "auth.log.2.gz", 5),
                  self.mk(L / "mysql/error.log.1", 90), self.mk(L / "journal/x.log.1", 90),
                  self.mk(self.fs / "home/forge/a.com/storage/logs/laravel.log", 90),
                  self.mk(self.fs / "home/forge/.composer/cache/a.zip", 90, 100)])
        self.mk(self.fs / "home/forge/a.com/artisan")
        self.mk(self.fs / "var/cache/apt/archives/a.deb", 0, 1000)
        self.runner.answers["journalctl --disk-usage"] = JOURNAL_1G

    def exists(self, key):
        return [p.exists() for p in self.paths[key]]

    def state(self):
        return core.read_json(self.ctx.path("state", "last-run.json"))

    def test_scan_is_read_only_and_lists_large_files(self):
        self.populate()
        self.mk(self.fs / "var/big.bin", 0, 2 * 1024 ** 2)
        self.write_config(large_file_mb=1)
        before = sorted(str(p) for p in self.fs.rglob("*"))
        rc, out, _ = self.cli("scan")
        self.assertEqual(rc, 0)
        self.assertIn(str(self.fs / "var/big.bin"), out)
        self.assertIn(str(self.fs), out)
        self.assertEqual(before, sorted(str(p) for p in self.fs.rglob("*")))
        self.assertFalse(self.runner.ran("apt-get clean") or self.runner.ran("journalctl --vacuum"))

    def test_scan_text_names_the_categories(self):
        self.populate()
        rc, out, err = self.cli("scan")
        self.assertEqual(rc, 0)
        self.assertIn("rotated", out)
        self.assertIn("Rotated logs", out)

    def test_run_accept_defaults_cleans_default_categories_only(self):
        self.populate()
        rc, out, err = self.cli("run", "--accept-defaults")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.exists("gone"), [False] * 4)
        self.assertEqual(self.exists("kept"), [True] * 7)
        self.assertTrue(self.runner.ran("apt-get clean") and self.runner.ran("journalctl --vacuum-size=500M"))
        s = self.state()
        self.assertEqual(set(s["categories"]), {"apt", "journal", "rotated", "laravel", "crash"})
        self.assertIn("freed_bytes", s)
        self.assertEqual(s["categories"]["rotated"]["files"], 2)

    def test_run_json_prints_the_result_document_only_on_stdout(self):
        self.populate()
        rc, out, err = self.cli("run", "--accept-defaults", "--json")
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["categories"]["rotated"]["files"], 2)
        self.assertEqual(doc["errors"], [])

    def test_run_only_caches(self):
        self.populate()
        rc, _, err = self.cli("run", "--only", "caches", "--accept-defaults")
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.paths["kept"][-1].exists())
        self.assertEqual(self.exists("gone"), [True] * 4)
        self.assertFalse(self.runner.ran("apt-get clean"))

    def test_run_unknown_category_is_usage_error(self):
        self.populate()
        rc, _, err = self.cli("run", "--only", "rotated,bogus", "--accept-defaults")
        self.assertEqual(rc, 64)
        self.assertIn("bogus", err)
        self.assertEqual(self.exists("gone"), [True] * 4)

    def test_run_without_an_answer_deletes_nothing(self):
        self.populate()
        rc, _, err = self.cli("run")
        self.assertEqual(rc, 64)
        self.assertEqual(self.exists("gone"), [True] * 4)

    def test_run_dry_run_deletes_nothing(self):
        self.populate()
        rc, out, err = self.cli("run", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.exists("gone"), [True] * 4)
        self.assertFalse(self.runner.ran("apt-get clean"))
        self.assertIn("rotated", out)

    def test_run_declined_deletes_nothing(self):
        self.populate()
        with mock.patch.object(clean.ui, "confirm", return_value=False):
            rc, _, _ = self.cli("run", "--only", "rotated")
        self.assertEqual(rc, 0)
        self.assertEqual(self.exists("gone"), [True] * 4)

    def test_one_category_failing_does_not_stop_the_others_and_exits_2(self):
        self.populate()
        self.runner.answers["apt-get clean"] = core.Result(100, "", "E: Could not get lock")
        rc, _, err = self.cli("run", "--accept-defaults")
        self.assertEqual(rc, 2)
        self.assertEqual(self.exists("gone"), [False] * 4)
        self.assertIn("lock", err)
        self.assertTrue(self.state()["errors"])

    def test_nothing_to_clean(self):
        rc, out, err = self.cli("run", "--accept-defaults")
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to clean", err)

    def test_run_is_exclusive(self):
        self.populate()
        with core.lock(self.ctx):
            rc, _, err = self.cli("run", "--accept-defaults")
        self.assertEqual(rc, 2)
        self.assertIn("lock", err)
        self.assertEqual(self.exists("gone"), [True] * 4)


class Setup(Base):
    def app(self):
        self.mk(self.fs / "home/forge/a.com/artisan")
        (self.fs / "home/forge/a.com/storage/logs").mkdir(parents=True)

    def test_writes_both_files_validates_reloads_and_is_idempotent(self):
        self.app()
        h = self.host()
        rc, out, err = self.cli("setup", "--accept-defaults", host=h)
        self.assertEqual(rc, 0, err)
        self.assertIn("SystemMaxUse=500M", Path(h.journald_conf).read_text())
        lr = Path(h.logrotate_conf).read_text()
        self.assertIn("a.com/storage/logs/laravel.log {", lr)
        self.assertIn("rotate 14", lr)
        self.assertTrue(self.runner.ran("systemctl restart systemd-journald"))
        self.assertTrue(self.runner.ran("systemctl is-active systemd-journald"))
        self.assertTrue(self.runner.ran("logrotate -d " + h.logrotate_conf))
        self.assertEqual(core.read_json(self.ctx.config_path)["journal_max"], "500M")
        self.runner.calls.clear()
        rc, _, err = self.cli("setup", "--accept-defaults", host=h)
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.runner.ran("systemctl restart"))

    def test_edited_values_are_written_and_remembered(self):
        self.app()
        h = self.host()
        env = {"SOFTSYNK_ANSWER_FINDING_JOURNALD": "edit", "SOFTSYNK_ANSWER_VALUE_JOURNALD": "1G",
               "SOFTSYNK_ANSWER_FINDING_LOGROTATE": "edit", "SOFTSYNK_ANSWER_VALUE_LOGROTATE": "21"}
        with mock.patch.dict(os.environ, env):
            rc, _, err = self.cli("setup", "--accept-defaults", host=h)
        self.assertEqual(rc, 0, err)
        self.assertIn("SystemMaxUse=1G", Path(h.journald_conf).read_text())
        self.assertIn("rotate 21", Path(h.logrotate_conf).read_text())
        c = core.read_json(self.ctx.config_path)
        self.assertEqual((c["journal_max"], c["laravel_rotate_days"]), ("1G", 21))

    def test_invalid_edit_is_refused(self):
        env = {"SOFTSYNK_ANSWER_FINDING_JOURNALD": "edit", "SOFTSYNK_ANSWER_VALUE_JOURNALD": "lots"}
        with mock.patch.dict(os.environ, env):
            rc, _, err = self.cli("setup")
        self.assertEqual(rc, 64)
        self.assertFalse(os.path.exists(self.host().journald_conf))

    def test_skipped_finding_writes_nothing(self):
        self.app()
        h = self.host()
        with mock.patch.dict(os.environ, {"SOFTSYNK_ANSWER_FINDING_JOURNALD": "skip", "SOFTSYNK_ANSWER_FINDING_LOGROTATE": "skip"}):
            rc, _, err = self.cli("setup", "--accept-defaults", host=h)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(h.journald_conf) or os.path.exists(h.logrotate_conf))

    def test_no_laravel_apps_means_no_logrotate_file(self):
        h = self.host()
        rc, _, err = self.cli("setup", "--accept-defaults", host=h)
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(h.journald_conf))
        self.assertFalse(os.path.exists(h.logrotate_conf))

    def test_failed_validation_rolls_back_that_file_only_and_exits_2(self):
        self.app()
        h = self.host()
        self.runner.answers["logrotate -d"] = core.Result(1, "", "error: bad config")
        rc, _, err = self.cli("setup", "--accept-defaults", host=h)
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(h.logrotate_conf))
        self.assertTrue(os.path.exists(h.journald_conf))
        self.assertIn("logrotate", err)

    def test_journald_that_will_not_run_is_rolled_back(self):
        h = self.host()
        self.runner.answers["systemctl is-active systemd-journald"] = 3
        rc, _, err = self.cli("setup", "--accept-defaults", host=h)
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(h.journald_conf))

    def test_dry_run_shows_the_diff_and_writes_nothing(self):
        self.app()
        h = self.host()
        rc, out, err = self.cli("setup", "--accept-defaults", "--dry-run", host=h)
        self.assertEqual(rc, 0, err)
        self.assertIn("SystemMaxUse=500M", out)
        self.assertFalse(os.path.exists(h.journald_conf) or os.path.exists(h.logrotate_conf))
        self.assertFalse(self.runner.ran("systemctl"))

    def test_engine_dry_run_env_writes_nothing(self):
        h = self.host()
        with mock.patch.dict(os.environ, {"SOFTSYNK_DRY_RUN": "1"}):
            rc, out, err = self.cli("setup", "--accept-defaults", host=h)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(h.journald_conf))
        self.assertFalse(self.runner.ran("systemctl"))


class Status(Base):
    def status(self, host=None):
        rc, out, _ = self.cli("--status-json", host=host)
        self.assertEqual(rc, 0)
        return json.loads(out)

    def test_not_installed(self):
        d = self.status()
        self.assertEqual((d["schema"], d["module"], d["install_state"], d["health"]), (1, "housekeeping", "not_installed", "unknown"))
        self.assertTrue(d["actions"])

    def test_installed_reports_last_run(self):
        h = self.host()
        self.mk(h.journald_conf)
        core.write_json(self.ctx.path("state", "last-run.json"),
                        {"at": "2026-11-03T10:00:00+00:00", "freed_bytes": 5 * 1024 ** 2, "estimated_bytes": 6 * 1024 ** 2,
                         "categories": {"rotated": {"files": 2, "bytes": 300, "errors": 0}}, "errors": []})
        d = self.status(h)
        self.assertEqual((d["install_state"], d["health"]), ("installed", "healthy"))
        self.assertEqual(d["details"]["last_run"]["freed_bytes"], 5 * 1024 ** 2)
        self.assertEqual(d["details"]["managed_files"], [h.journald_conf])
        self.assertIn("5.0 MiB", d["summary"])

    def test_failed_last_run_and_corrupt_files_degrade_but_exit_zero(self):
        h = self.host()
        self.mk(h.journald_conf)
        core.write_json(self.ctx.path("state", "last-run.json"), {"at": "2026-11-03T10:00:00+00:00", "freed_bytes": 0, "errors": ["apt: lock"]})
        d = self.status(h)
        self.assertEqual(d["health"], "degraded")
        self.assertTrue(any("apt" in w for w in d["warnings"]))
        Path(self.ctx.path("state", "last-run.json")).write_text("{not json")
        self.assertEqual(self.status(h)["health"], "degraded")
        Path(self.ctx.config_path).write_text("[]")
        self.assertEqual(self.status(h)["health"], "degraded")

    def test_never_run_is_healthy(self):
        h = self.host()
        self.mk(h.logrotate_conf)
        d = self.status(h)
        self.assertEqual(d["health"], "healthy")
        self.assertIn("never", d["summary"])


if __name__ == "__main__":
    unittest.main()
