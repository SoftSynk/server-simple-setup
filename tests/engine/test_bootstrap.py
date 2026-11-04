"""bootstrap.sh, update and rollback against a temporary root."""
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest

import support
from support import Base, bootstrap, engine, tarball, tree_hash

AD = ["--accept-defaults"]


class BootstrapBase(Base):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.env = {"SOFTSYNK_BIN_DIR": str(self.bin), "SOFTSYNK_OS_RELEASE": support.OS_RELEASE["ubuntu-24.04"]}
        self.v1 = self.release(["alpha", "beta"], name="v1", version="0.1.0")

    def boot(self, src=None, *args):
        return bootstrap(src or self.v1, ["--root", str(self.root), *args], self.env)

    def link(self, name):
        return os.readlink(self.root / name) if (self.root / name).is_symlink() else None

    def releases(self):
        return sorted(p.name for p in (self.root / "releases").iterdir())

    def run_engine(self, *args):
        return engine(self.root / "current", list(args), self.root, self.env)


class Bootstrap(BootstrapBase):
    def test_first_install_lays_out_the_root(self):
        r = self.boot()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.link("current"), "releases/0.1.0")
        self.assertIsNone(self.link("previous"))
        self.assertEqual(self.releases(), ["0.1.0"])
        modes = {d: stat.S_IMODE((self.root / d).stat().st_mode)
                 for d in ("releases", "etc", "state", "log", "data", "secrets", "tmp")}
        self.assertEqual(modes, {"releases": 0o755, "etc": 0o755, "state": 0o755, "log": 0o755,
                                 "data": 0o755, "secrets": 0o700, "tmp": 0o700})
        self.assertTrue((self.root / "lock" / "framework.lock").exists())
        self.assertEqual((self.bin / "softsynk-server").resolve(), (self.root / "releases/0.1.0/softsynk-server").resolve())
        self.assertFalse(os.access(self.root / "releases/0.1.0/VERSION", os.W_OK))  # staged read-only
        v = self.run_engine("--version")
        self.assertEqual(v.stdout.strip(), "softsynk-server 0.1.0 (module interface 1)")
        self.assertEqual(self.run_engine("validate").returncode, 0)

    def test_rerun_is_idempotent(self):
        self.boot()
        before = tree_hash(self.root / "releases")
        r = self.boot()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already active", r.stdout)
        self.assertEqual(tree_hash(self.root / "releases"), before)
        self.assertIsNone(self.link("previous"))

    def test_manifest_commands_are_linked_once_a_release_ships_them(self):
        self.boot()
        self.assertEqual(sorted(p.name for p in self.bin.iterdir()), ["softsynk-server"])
        v2 = self.release(["alpha", "cmds"], name="v2", version="0.2.0")
        self.assertEqual(self.boot(v2).returncode, 0)
        cur = self.root / "current"
        self.assertEqual(os.readlink(self.bin / "synk-one"), str(cur / "deployment/cmds/one"))
        self.assertEqual(os.readlink(self.bin / "synk-two"), str(cur / "deployment/cmds/sub/two"))

    def test_foreign_command_link_is_left_alone(self):
        self.bin.mkdir()
        (self.bin / "softsynk-server").symlink_to("/bin/echo")
        r = self.boot()
        self.assertEqual(r.returncode, 0)
        self.assertIn("points elsewhere", r.stderr)
        self.assertEqual(os.readlink(self.bin / "softsynk-server"), "/bin/echo")

    def test_foreign_manifest_command_link_is_left_alone(self):
        self.bin.mkdir()
        (self.bin / "synk-one").symlink_to("/bin/echo")
        r = self.boot(self.release(["alpha", "cmds"], name="v2", version="0.2.0"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("synk-one points elsewhere", r.stderr)
        self.assertEqual(os.readlink(self.bin / "synk-one"), "/bin/echo")
        self.assertTrue((self.bin / "synk-two").is_symlink())  # the other command is still linked

    def test_usage_errors_exit_64(self):
        for args in (["--root", "relative/path"], ["--sha256", "abc"], ["--from", str(self.tmp / "nope")],
                     ["--from", str(self.v1), "--sha256", support.ZERO_SHA], ["--bogus"]):
            with self.subTest(args=args):
                r = bootstrap(self.v1, args, self.env)
                self.assertEqual(r.returncode, 64, r.stderr)

    def test_non_ubuntu_warns_and_install_flag_does_not_apt(self):
        self.env["SOFTSYNK_OS_RELEASE"] = support.OS_RELEASE["debian-12"]
        # --install hands over to `softsynk-server install`, which needs names or a terminal: 64 proves the handover
        r = self.boot(None, "--install")
        self.assertIn("not Ubuntu", r.stderr)
        self.assertEqual(r.returncode, 64, r.stdout + r.stderr)
        self.assertEqual(self.link("current"), "releases/0.1.0")

    def _apt_env(self, install_fails):
        """PATH without gum, a root `id`, and an apt-get that records its calls (`install` fails `install_fails` times)."""
        shims, farm, log = self.tmp / "shims", self.tmp / "farm", self.tmp / "apt.log"
        shims.mkdir()
        farm.mkdir()
        (shims / "id").write_text("#!/bin/sh\necho 0\n")
        (shims / "apt-get").write_text(
            '#!/bin/sh\n'
            f'echo "$DEBIAN_FRONTEND $*" >> {log}\n'
            'if [ "$1" = install ]; then\n'
            f'  n=$(cat {self.tmp}/n 2>/dev/null || echo 0); echo $((n + 1)) > {self.tmp}/n\n'
            f'  [ "$n" -ge {install_fails} ]\n'
            'fi\n')
        for f in shims.iterdir():
            f.chmod(0o755)
        for d in os.environ["PATH"].split(os.pathsep):  # every tool except gum, id and apt-get
            for e in (os.scandir(d) if os.path.isdir(d) else ()):
                if e.name not in ("gum", "id", "apt-get") and not (farm / e.name).exists() and os.access(e.path, os.X_OK):
                    os.symlink(e.path, farm / e.name)
        self.env["PATH"] = f"{shims}{os.pathsep}{farm}"
        return log

    def test_install_gum_updates_the_package_index_and_retries(self):
        log = self._apt_env(install_fails=1)
        r = self.boot(None, "--install")
        self.assertNotIn("apt-get install gum failed", r.stderr)
        self.assertEqual(r.returncode, 64, r.stdout + r.stderr)  # past the gum step, at the engine handover
        self.assertEqual(log.read_text().splitlines(),
                         ["noninteractive install -y gum", " update", "noninteractive install -y gum"])

    def test_install_gum_failing_twice_is_a_clear_error(self):
        self._apt_env(install_fails=9)
        r = self.boot(None, "--install")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("apt-get install gum failed", r.stderr)

    def test_refused_while_another_command_holds_the_lock(self):
        rel = self.release(["holdinstall"], name="hold")
        first = subprocess.Popen([sys.executable, str(rel / "softsynk-server"), "--root", str(self.root),
                                  "install", "holdinstall", *AD], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 20
            while not (self.root / "hold.marker").exists():
                self.assertLess(time.time(), deadline)
                time.sleep(0.05)
            r = self.boot()
            self.assertEqual(r.returncode, 2)
            self.assertIn("another softsynk-server command is running", r.stderr)
            self.assertFalse((self.root / "current").exists())
        finally:
            first.wait(timeout=30)


class UpdateRollback(BootstrapBase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.boot().returncode, 0)
        self.v2 = self.release(["alpha", "beta"], name="v2", version="0.2.0")

    def update(self, *args):
        return self.run_engine("update", *args, *AD)

    def test_update_then_rollback_restores_the_previous_release(self):
        r = self.update("--from", str(self.v2))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.link("current"), self.link("previous")), ("releases/0.2.0", "releases/0.1.0"))
        self.assertEqual(self.releases(), ["0.1.0", "0.2.0"])
        self.assertEqual(self.run_engine("--version").stdout.split()[1], "0.2.0")

        r = self.run_engine("--rollback", *AD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.link("current"), self.link("previous")), ("releases/0.1.0", "releases/0.2.0"))
        self.assertEqual(self.run_engine("--version").stdout.split()[1], "0.1.0")
        self.assertIn("only code is reverted", r.stdout)

        self.run_engine("--rollback", *AD)  # and forward again
        self.assertEqual(self.link("current"), "releases/0.2.0")

    def test_update_and_rollback_are_recorded_in_history(self):
        self.update("--from", str(self.v2))
        self.run_engine("--rollback", *AD)
        lines = (self.root / "state/framework/history.jsonl").read_text().splitlines()
        commands = [json.loads(x)["command"] for x in lines]
        self.assertEqual(commands, ["update", "rollback"])

    def test_update_from_tarball_with_correct_sha256(self):
        tar = self.tmp / "release.tar.gz"
        sha = tarball(self.v2, tar)
        r = self.update("--from", str(tar), "--sha256", sha)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("sha256 verified", r.stdout)
        self.assertEqual(self.link("current"), "releases/0.2.0")
        self.assertEqual(self.run_engine("validate").returncode, 0)

    def test_wrong_sha256_is_refused_and_nothing_is_switched(self):
        tar = self.tmp / "release.tar.gz"
        tarball(self.v2, tar)
        before = (tree_hash(self.root / "releases"), self.link("current"), self.link("previous"))
        r = self.update("--from", str(tar), "--sha256", support.ZERO_SHA)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("sha256 mismatch", r.stderr)
        self.assertEqual((tree_hash(self.root / "releases"), self.link("current"), self.link("previous")), before)
        self.assertEqual(self.releases(), ["0.1.0"])  # no staging or extract leftovers

    def test_same_wrong_sha256_via_bootstrap_directly(self):
        tar = self.tmp / "release.tar.gz"
        tarball(self.v2, tar)
        r = self.boot(None, "--from", str(tar), "--sha256", support.ZERO_SHA)
        self.assertEqual(r.returncode, 2)
        self.assertEqual((self.link("current"), self.releases()), ("releases/0.1.0", ["0.1.0"]))

    def test_tarball_without_sha256_is_a_usage_error(self):
        tar = self.tmp / "release.tar.gz"
        tarball(self.v2, tar)
        r = self.run_engine("update", "--from", str(tar), *AD)
        self.assertEqual(r.returncode, 64)
        self.assertIn("--sha256 is required", r.stderr)
        r = self.boot(None, "--from", str(tar))
        self.assertEqual(r.returncode, 64)

    def test_release_with_an_invalid_manifest_is_refused_before_switching(self):
        bad = self.release(["alpha", "bad-shell-string"], name="bad", version="0.3.0")
        r = self.update("--from", str(bad))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("failed manifest validation", r.stderr)
        self.assertEqual((self.link("current"), self.releases()), ("releases/0.1.0", ["0.1.0"]))

    def test_same_version_with_different_contents_is_refused(self):
        other = self.release(["alpha"], name="other", version="0.1.0")
        r = self.update("--from", str(other))
        self.assertEqual(r.returncode, 2)
        self.assertIn("exists with different contents", r.stderr)
        self.assertEqual(self.link("current"), "releases/0.1.0")

    def test_rollback_without_a_previous_release_fails_cleanly(self):
        r = self.run_engine("--rollback", *AD)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no previous release", r.stderr)
        self.assertEqual(self.link("current"), "releases/0.1.0")

    def test_rollback_on_an_uninstalled_root_creates_nothing(self):
        r = engine(self.v1, ["--rollback", *AD], self.tmp / "never-installed")
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.tmp / "never-installed").exists())

    def test_rollback_needs_confirmation_non_interactively(self):
        self.update("--from", str(self.v2))
        r = self.run_engine("--rollback")
        self.assertEqual(r.returncode, 64)
        self.assertEqual(self.link("current"), "releases/0.2.0")

    def test_rollback_refuses_a_previous_link_that_leaves_releases(self):
        self.update("--from", str(self.v2))
        (self.root / "previous").unlink()
        (self.root / "previous").symlink_to("/tmp")
        r = self.run_engine("--rollback", *AD)
        self.assertEqual(r.returncode, 2)
        self.assertIn("missing or outside", r.stderr)
        self.assertEqual(self.link("current"), "releases/0.2.0")


class StaticChecks(unittest.TestCase):
    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck_clean(self):
        files = [support.REPO / "bootstrap.sh", support.FIXTURES / "modules/_common/run.sh"]
        r = subprocess.run(["shellcheck", *map(str, files)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_scripts_are_executable_and_engine_compiles(self):
        for name in ("softsynk-server", "bootstrap.sh"):
            self.assertTrue(os.access(support.REPO / name, os.X_OK), name)
        compile((support.REPO / "softsynk-server").read_text(), "softsynk-server", "exec")


if __name__ == "__main__":
    unittest.main()
