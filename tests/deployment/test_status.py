"""setup-redis.sh and setup-auto-updates.sh: the module contract (--status-json, non-interactive flags).

No root, systemd, apt or Redis is needed. The Ubuntu code paths run on any machine through a temp copy of
the script in which the fixed system paths (/etc/os-release, the apt/Redis config files) are text-replaced,
with stub systemctl, dpkg-query, apt-config, redis-cli and friends first on PATH. Production code carries no
test hooks. Unverified here: a real Ubuntu/Debian server, real systemd and a live Redis.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
BASHES = [b for b in (shutil.which("bash"), "/bin/bash") if b and os.path.exists(b)]
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"  # no Homebrew: a developer's own redis/gum must not leak in
UBUNTU = 'ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu 26.04 LTS"\n'
DEBIAN = 'ID=debian\nPRETTY_NAME="Debian GNU/Linux 12"\n'
ALPINE = 'ID=alpine\nPRETTY_NAME="Alpine Linux"\n'
PW = "Sup3r-secret-redis-password-77"
KEYS = {"schema", "module", "version", "observed_at", "install_state", "health", "summary", "details",
        "warnings", "actions"}
INSTALL_STATES = {"installed", "adopted", "not_installed", "unknown"}
HEALTHS = {"healthy", "degraded", "unhealthy", "unknown", "stale", "unsupported"}

# Stub programs, all bash 3.2 safe. They answer from files in $STUB_DIR and log every call.
STUBS = {
    "systemctl": '''echo "systemctl $*" >> "$STUB_DIR/calls.log"
case $1 in
  is-active) cat "$STUB_DIR/sd/active.$2" 2>/dev/null ;;
  is-enabled) cat "$STUB_DIR/sd/enabled.$2" 2>/dev/null ;;
  show) cat "$STUB_DIR/sd/show.$3" 2>/dev/null ;;
esac
exit 0''',
    "dpkg-query": '''[ -f "$STUB_DIR/uu_version" ] || exit 1
case $* in *Status*) printf 'install ok installed' ;; *) cat "$STUB_DIR/uu_version" ;; esac''',
    "apt-config": '''[ -f "$STUB_DIR/apt-dump" ] && cat "$STUB_DIR/apt-dump" || exit 1''',
    "timeout": 'shift; exec "$@"',
    "sysctl": 'cat "$STUB_DIR/overcommit" 2>/dev/null; exit 0',
    "redis-server": 'echo "Redis server v=7.2.4 sha=00000000:0 malloc=jemalloc-5.3.0 bits=64 build=0"',
    # redis-cli -h 127.0.0.1 -p PORT COMMAND...; the password arrives in REDISCLI_AUTH.
    "redis-cli": '''port=$4; shift 4
echo "$port $*" >> "$STUB_DIR/redis.log"
printf '%s' "${REDISCLI_AUTH-}" > "$STUB_DIR/auth.$port"
case $1 in
  PING) [ -f "$STUB_DIR/down.$port" ] || echo PONG ;;
  INFO) cat "$STUB_DIR/info.$port" 2>/dev/null ;;
  CONFIG) if [ -f "$STUB_DIR/cfg.$port.$3" ]; then echo "$3"; cat "$STUB_DIR/cfg.$port.$3"; fi ;;
esac
exit 0''',
}
# Anything that installs or runs an upgrade: --status-json must never reach these.
TRIPWIRES = ("unattended-upgrade", "apt-get", "apt", "apt-cache", "flock", "gum")
INFO_OK = "# Memory\r\nused_memory:1000\r\nmaxmemory:268435456\r\n# Persistence\r\naof_enabled:0\r\n" \
          "rdb_last_bgsave_status:ok\r\n# Keyspace\r\ndb0:keys=5,expires=0,avg_ttl=0\r\n"
APT_OK = ('APT::Periodic::Enable "1";\nAPT::Periodic::Unattended-Upgrade "1";\n'
          'Unattended-Upgrade::Automatic-Reboot "true";\nUnattended-Upgrade::Automatic-Reboot-Time "03:30";\n')
MANAGED = ("// Managed by setup-auto-updates.sh 1.0.0. Rerun the script to change these settings;\n"
           "//@ scope=updates\n//@ third_party=php\n//@ reboot=03:30\n//@ mail=\n")


def tree_hash(path):
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for n in sorted(dirnames + filenames):
            p = os.path.join(dirpath, n)
            st = os.lstat(p)
            h.update(f"{os.path.relpath(p, path)}|{st.st_mode}|".encode())
            if os.path.isfile(p) and not os.path.islink(p):
                try:
                    with open(p, "rb") as f:
                        h.update(f.read())
                except OSError:  # a deliberately unreadable fixture file: the mode above still counts
                    pass
    return h.hexdigest()


def check_doc(test, doc, mid):
    """The status contract, as the engine checks it (softsynk-server check_status_doc) plus its types."""
    test.assertEqual(set(doc), KEYS)
    test.assertEqual((doc["schema"], doc["module"]), (1, mid))
    test.assertIn(doc["install_state"], INSTALL_STATES)
    test.assertIn(doc["health"], HEALTHS)
    test.assertRegex(doc["observed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
    for k in ("version", "summary"):
        test.assertIsInstance(doc[k], str)
    test.assertIsInstance(doc["details"], dict)
    for k in ("warnings", "actions"):
        test.assertTrue(isinstance(doc[k], list) and all(isinstance(x, str) for x in doc[k]), k)


class Base(unittest.TestCase):
    SCRIPT = ""  # path below deployment/
    MODULE = ""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deploy-test-")
        self.addCleanup(self.rm)
        j = lambda *p: os.path.join(self.tmp, *p)  # noqa: E731
        self.tree, self.root, self.stub_dir = j("tree"), j("root"), j("stub")
        self.etc, self.osrel, self.reboot = j("etc"), j("os-release"), j("reboot-required")
        for d in (self.root, self.etc, os.path.join(self.stub_dir, "sd")):
            os.makedirs(d)
        os.makedirs(os.path.join(self.tree, "lib"))
        shutil.copy(os.path.join(REPO, "lib", "ui.sh"), os.path.join(self.tree, "lib", "ui.sh"))
        self.script = None

    def rm(self):
        subprocess.run(["chmod", "-R", "u+rwx", self.tmp], check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install_script(self, subs):
        """Copy the real script next to lib/ui.sh, replacing system paths. Each replacement must apply."""
        src = os.path.join(REPO, "deployment", self.SCRIPT)
        with open(src) as f:
            text = f.read()
        for old, new in subs:
            self.assertIn(old, text, f"test substitution no longer matches the script: {old}")
            text = text.replace(old, new)
        dest = os.path.join(self.tree, "deployment", self.SCRIPT)
        os.makedirs(os.path.dirname(dest))
        with open(dest, "w") as f:
            f.write(text)
        self.script = dest

    def stub(self, name, body=None):
        p = os.path.join(self.stub_dir, name)
        with open(p, "w") as f:
            f.write("#!/bin/bash\n" + (body or f'echo "$0 $*" >> "$STUB_DIR/tripwire.log"; exit 0') + "\n")
        os.chmod(p, 0o755)

    def put(self, rel, text, base=None):
        p = os.path.join(base or self.stub_dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def stubs(self, names):
        for n in names:
            self.stub(n, STUBS[n])
        for n in TRIPWIRES:
            self.stub(n)

    def run_script(self, args, bash, script=None, env=None, path=None):
        e = {k: v for k, v in os.environ.items() if not k.startswith("SOFTSYNK_")}
        e.update(PATH=path or f"{self.stub_dir}:{SYSTEM_PATH}", STUB_DIR=self.stub_dir)
        e.update(env or {})
        return subprocess.run([bash, script or self.script, *args], capture_output=True, text=True, env=e,
                              stdin=subprocess.DEVNULL, timeout=60)

    def status(self, os_release=UBUNTU):
        """Run --status-json under every bash: exit 0, valid contract, same document, nothing written."""
        self.put("os-release", os_release, self.tmp)
        before = [tree_hash(d) for d in (self.root, self.etc, self.tree)]
        docs = []
        for b in BASHES:
            r = self.run_script(["--status-json", "--root", self.root], b)
            self.assertEqual(r.returncode, 0, (b, r.stderr))
            self.assertEqual(r.stderr, "", b)
            doc = json.loads(r.stdout)
            check_doc(self, doc, self.MODULE)
            docs.append(doc)
        self.assertEqual([tree_hash(d) for d in (self.root, self.etc, self.tree)], before, "status wrote files")
        self.assertFalse(os.path.exists(os.path.join(self.stub_dir, "tripwire.log")), "status ran an installer")
        calls = self.read(os.path.join(self.stub_dir, "calls.log"))
        self.assertFalse(re.search(r"systemctl (?!is-active|is-enabled|show)", calls), calls)
        for d in docs:
            d.pop("observed_at")
        self.assertTrue(all(d == docs[0] for d in docs), "bash versions disagree")
        return docs[0]

    @staticmethod
    def read(p):
        try:
            with open(p) as f:
                return f.read()
        except OSError:
            return ""

    def assertNoPassword(self, doc):
        self.assertNotIn(PW, json.dumps(doc))


class CommonTests:
    """Run for each script against the unmodified file in the repo."""

    def real(self, *args, bash=None, env=None):
        s = os.path.join(REPO, "deployment", self.SCRIPT)
        return self.run_script(list(args), bash or BASHES[0], script=s, path=os.environ["PATH"], env=env)

    def test_help_lists_the_contract_flags(self):
        r = self.real("--help")
        self.assertEqual(r.returncode, 0)
        for flag in ("--status-json", "--non-interactive", "--accept-defaults", "--dry-run", "--self-test"):
            self.assertIn(flag, r.stdout)

    def test_self_test_passes_under_every_bash(self):
        for b in BASHES:
            r = self.real("--self-test", bash=b)
            self.assertEqual(r.returncode, 0, (b, r.stdout[-400:], r.stderr[-400:]))

    def test_unknown_option_and_missing_value_exit_64(self):
        self.assertEqual(self.real("--bogus").returncode, 64)
        self.assertEqual(self.real("--root").returncode, 64)

    def test_real_host_status_is_valid_and_read_only(self):
        before = tree_hash(self.root)
        for b in BASHES:
            r = self.real("--status-json", "--root", self.root, bash=b)
            self.assertEqual(r.returncode, 0, r.stderr)
            check_doc(self, json.loads(r.stdout), self.MODULE)
        self.assertEqual(tree_hash(self.root), before)

    def test_non_interactive_dry_run_stops_at_the_first_prompt_with_64(self):
        before = tree_hash(self.root)
        for b in BASHES:
            r = self.real("--dry-run", "--non-interactive", "--root", self.root, bash=b)
            self.assertEqual(r.returncode, 64, (b, r.stdout[-300:], r.stderr[-300:]))
        self.assertEqual(tree_hash(self.root), before, "a dry run wrote under the root")

    def test_unsupported_os_is_reported_not_failed(self):
        doc = self.status(ALPINE)
        self.assertEqual((doc["install_state"], doc["health"]), ("not_installed", "unsupported"))


class AutoUpdatesTests(CommonTests, Base):
    SCRIPT, MODULE = "auto-updates/setup-auto-updates.sh", "auto-updates"
    STUB_NAMES = ("systemctl", "dpkg-query", "apt-config")

    def setUp(self):
        super().setUp()
        self.install_script([
            ("readonly CONF=/etc/apt/apt.conf.d/52unattended-upgrades-local", f"readonly CONF={self.etc}/52-local"),
            ("/etc/os-release", self.osrel),
            ("/var/run/reboot-required", self.reboot),
        ])
        self.conf = f"{self.etc}/52-local"
        self.stubs(self.STUB_NAMES)

    def healthy(self):
        self.put("uu_version", "2.12")
        self.put("apt-dump", APT_OK)
        for t in ("apt-daily.timer", "apt-daily-upgrade.timer"):
            self.put(f"sd/active.{t}", "active\n")
        self.put("sd/show.NextElapseUSecRealtime", "Wed 2026-11-04 06:10:00 UTC\n")
        self.put("sd/show.LastTriggerUSec", "n/a\n")

    def test_not_installed(self):
        doc = self.status()
        self.assertEqual((doc["install_state"], doc["health"], doc["summary"]), ("not_installed", "unknown", "not installed"))
        self.assertEqual(doc["actions"], ["Run setup-auto-updates.sh."])

    def test_managed_and_healthy(self):
        self.healthy()
        self.put("52-local", MANAGED, self.etc)
        doc = self.status()
        self.assertEqual((doc["install_state"], doc["health"]), ("installed", "healthy"))
        self.assertIn("security + stable updates", doc["summary"])
        self.assertEqual(doc["details"]["scope"], "updates")
        self.assertEqual((doc["details"]["auto_upgrade"], doc["details"]["reboot_required"]), (True, False))

    def test_daily_upgrades_off_is_unhealthy(self):
        self.healthy()
        self.put("52-local", MANAGED, self.etc)
        self.put("apt-dump", APT_OK.replace('Unattended-Upgrade "1"', 'Unattended-Upgrade "0"'))
        doc = self.status()
        self.assertEqual(doc["health"], "unhealthy")
        self.assertIn("daily upgrades are off", doc["summary"])

    def test_reboot_setting_overridden_by_a_later_file_is_unhealthy(self):
        self.healthy()
        self.put("52-local", MANAGED, self.etc)
        self.put("apt-dump", APT_OK.replace('Automatic-Reboot "true"', 'Automatic-Reboot "false"'))
        self.assertEqual(self.status()["health"], "unhealthy")

    def test_pending_reboot_with_automatic_reboot_off_is_degraded(self):
        self.healthy()
        self.put("52-local", MANAGED.replace("reboot=03:30", "reboot=off"), self.etc)
        self.put("apt-dump", APT_OK.replace('Automatic-Reboot "true"', 'Automatic-Reboot "false"'))
        self.put("reboot-required", "", self.tmp)
        doc = self.status()
        self.assertEqual(doc["health"], "degraded")
        self.assertTrue(doc["details"]["reboot_required"])
        self.assertIn("Reboot the server when convenient.", doc["actions"])

    def test_inactive_timer_is_unhealthy_and_silent_systemd_is_unknown(self):
        self.healthy()
        self.put("52-local", MANAGED, self.etc)
        self.put("sd/active.apt-daily.timer", "inactive\n")
        self.assertEqual(self.status()["health"], "unhealthy")
        os.remove(os.path.join(self.stub_dir, "sd", "active.apt-daily.timer"))
        self.assertEqual(self.status()["health"], "unknown")

    def test_installed_package_without_our_config_is_adopted(self):
        self.healthy()
        doc = self.status()
        self.assertEqual(doc["install_state"], "adopted")
        self.assertIn("Run setup-auto-updates.sh to manage these settings.", doc["actions"])

    def test_unreadable_apt_config_is_unknown_not_a_crash(self):
        self.healthy()
        self.put("52-local", MANAGED, self.etc)
        os.remove(os.path.join(self.stub_dir, "apt-dump"))
        self.assertEqual(self.status()["health"], "unknown")

    def test_debian_is_unsupported(self):
        self.assertEqual(self.status(DEBIAN)["health"], "unsupported")


class RedisTests(CommonTests, Base):
    SCRIPT, MODULE = "redis-setup/setup-redis.sh", "redis"
    STUB_NAMES = ("systemctl", "redis-cli", "redis-server", "timeout", "sysctl")

    def setUp(self):
        super().setUp()
        self.install_script([
            ("readonly ETC_DIR=/etc/redis", f"readonly ETC_DIR={self.etc}"),
            ("readonly STOCK_CONF=/etc/redis/redis.conf", f"readonly STOCK_CONF={self.etc}/redis.conf"),
            ("/etc/os-release", self.osrel),
        ])
        self.stubs(self.STUB_NAMES)

    def instance(self, name="default", port=6379, role="cache", extra="", info=INFO_OK, active=True):
        conf = "redis.conf" if name == "default" else f"redis-{name}.conf"
        unit = "redis-server.service" if name == "default" else f"redis-server@{name}.service"
        self.put(conf, f"port {port}\n{extra}" + (f"# >>> managed\n# role={role}\n# <<<\n" if role else ""), self.etc)
        self.put(f"sd/active.{unit}", "active\n" if active else "inactive\n")
        self.put(f"sd/enabled.{unit}", "enabled\n")
        self.put(f"info.{port}", info)

    def test_not_installed(self):
        if shutil.which("redis-server", path=SYSTEM_PATH):
            self.skipTest("redis-server is installed on this machine")
        os.remove(os.path.join(self.stub_dir, "redis-server"))
        doc = self.status()
        self.assertEqual((doc["install_state"], doc["health"], doc["summary"]), ("not_installed", "unknown", "not installed"))

    def test_single_instance_healthy(self):
        self.instance()
        doc = self.status()
        self.assertEqual((doc["install_state"], doc["health"]), ("installed", "healthy"))
        self.assertEqual(doc["summary"], "1 of 1 instance(s) answering: default:6379")
        d = doc["details"]
        self.assertEqual((d["redis_version"], d["default.role"], d["default.keys"]), ("7.2.4", "cache", "5"))
        self.assertIs(d["default.password_set"], False)

    def test_dual_instance_and_only_read_commands(self):
        self.instance()
        self.instance("queue", 6380, "queue")
        doc = self.status()
        self.assertEqual(doc["summary"], "2 of 2 instance(s) answering: default:6379, queue:6380")
        cmds = {line.split(" ", 1)[1].split(" ")[0] for line in self.read(f"{self.stub_dir}/redis.log").splitlines()}
        self.assertTrue(cmds <= {"PING", "INFO", "CONFIG"}, cmds)
        self.assertNotIn("CONFIG SET", self.read(f"{self.stub_dir}/redis.log"))

    def test_password_is_used_but_never_printed(self):
        self.instance(extra=f"requirepass {PW}\n")
        doc = self.status()
        self.assertIs(doc["details"]["default.password_set"], True)
        self.assertEqual(self.read(f"{self.stub_dir}/auth.6379"), PW)  # it reached redis-cli by environment
        self.assertNoPassword(doc)

    def test_failed_rdb_save_is_unhealthy(self):
        self.instance(info=INFO_OK.replace("rdb_last_bgsave_status:ok", "rdb_last_bgsave_status:err"))
        self.assertEqual(self.status()["health"], "unhealthy")

    def test_over_90_percent_of_maxmemory_is_degraded(self):
        self.instance(info=INFO_OK.replace("used_memory:1000", "used_memory:250000000"))
        self.assertEqual(self.status()["health"], "degraded")

    def test_stopped_enabled_instance_is_unhealthy(self):
        self.instance(active=False)
        doc = self.status()
        self.assertEqual(doc["health"], "unhealthy")
        self.assertIn("redis-server.service is inactive", doc["summary"])

    def test_active_but_not_answering_is_unhealthy(self):
        self.instance()
        self.put("down.6379", "")
        self.assertEqual(self.status()["health"], "unhealthy")

    def test_public_bind_without_password_is_unhealthy(self):
        self.instance()
        self.put("cfg.6379.bind", "0.0.0.0\n")
        self.put("cfg.6379.protected-mode", "no\n")
        self.assertEqual(self.status()["health"], "unhealthy")

    def test_instance_not_written_by_the_script_is_adopted(self):
        self.instance(role="")
        doc = self.status()
        self.assertEqual((doc["install_state"], doc["health"]), ("adopted", "healthy"))
        self.assertIn("Run setup-redis.sh to manage these Redis settings.", doc["actions"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads everything")
    def test_unreadable_config_is_unknown_not_a_crash(self):
        self.instance(extra=f"requirepass {PW}\n")
        os.chmod(os.path.join(self.etc, "redis.conf"), 0)
        doc = self.status()
        self.assertEqual((doc["install_state"], doc["health"]), ("unknown", "unknown"))
        self.assertIn("cannot read", doc["summary"])
        self.assertNoPassword(doc)

    def test_redis_cli_missing_is_unknown(self):
        if shutil.which("redis-cli", path=SYSTEM_PATH):
            self.skipTest("redis-cli is installed on this machine")
        self.instance()
        os.remove(os.path.join(self.stub_dir, "redis-cli"))
        self.assertEqual(self.status()["health"], "unknown")


class EngineContract(unittest.TestCase):
    """The engine accepts both manifests and parses both scripts' real --status-json output."""

    def test_engine_reads_the_status_of_both_modules(self):
        tmp = tempfile.mkdtemp(prefix="deploy-engine-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rel = os.path.join(tmp, "release")
        shutil.copytree(os.path.join(REPO, "lib"), os.path.join(rel, "lib"))
        for m in ("redis-setup", "auto-updates"):
            shutil.copytree(os.path.join(REPO, "deployment", m), os.path.join(rel, "deployment", m))
        for f in ("softsynk-server", "VERSION"):
            shutil.copy2(os.path.join(REPO, f), rel)
        with open(os.path.join(rel, "modules.toml"), "w") as f:
            f.write('schema = 1\n[[modules]]\nid = "redis"\nmanifest = "deployment/redis-setup/module.toml"\n'
                    '[[modules]]\nid = "auto-updates"\nmanifest = "deployment/auto-updates/module.toml"\n')
        e = {k: v for k, v in os.environ.items() if not k.startswith("SOFTSYNK_")}
        e["SOFTSYNK_OS_RELEASE"] = os.path.join(REPO, "tests", "fixtures", "os-release.ubuntu-24.04")
        r = subprocess.run([sys.executable, os.path.join(rel, "softsynk-server"), "--root", os.path.join(tmp, "root"),
                            "--status", "--json"], capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL,
                           timeout=120)
        doc = json.loads(r.stdout)
        self.assertEqual([m["module"] for m in doc["modules"]], ["redis", "auto-updates"])
        for m in doc["modules"]:
            self.assertNotEqual(m["summary"], "status unreadable", m)
            self.assertNotIn("status could not be started", json.dumps(m))


if __name__ == "__main__":
    unittest.main()
