"""setup-backup.sh wiring: answers and secrets reach `synk-backup configure` correctly.

The real synk-backup is replaced by a stub that records argv, stdin and environment, so this runs anywhere
(no root, no systemd, no restic). The root/Ubuntu/systemd parts are covered by the non-root refusal test and
are listed as unverified in the report.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
BASHES = [b for b in (shutil.which("bash"), "/bin/bash") if b and os.path.exists(b)]
PW = "Sup3r-secret-password-1234"
SEC = "S3-secret-access-key-ZZZ987"
KID = "AKIAFAKEKEY1234"

STUB = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["STUB_DIR"]
a = sys.argv[1:]
if a[:2] == ["--root", a[1]]:
    a = a[2:]
cmd = a[0]
if cmd == "config":
    print(os.environ.get("STUB_CONFIG") or json.dumps({
        "configured": False, "has_password": False, "has_s3_keys": False,
        "defaults": {"local_repo": "x", "prefix": "softsynk-testhost"},
        "config": {"local_repo": os.environ["STUB_DEFAULT_REPO"], "remote": None, "mail_to": "",
                   "retention": {"daily": 3, "weekly": 1, "twice_monthly": 2}, "exclude_databases": []}}))
elif cmd == "genpass":
    print("generated-password-0123456789")
elif cmd == "configure":
    with open(os.path.join(d, "configure.json"), "w") as f:
        json.dump({"argv": a, "stdin": sys.stdin.read(),
                   "answer_env": sorted(k for k in os.environ if k.startswith("SOFTSYNK_ANSWER_"))}, f)
    sys.exit(int(os.environ.get("STUB_CONFIGURE_RC", "0")))
elif a[0] == "--status":
    print("healthy")
'''


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="synk-setup-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mod = os.path.join(self.tmp, "tree", "deployment", "backup")
        os.makedirs(os.path.join(self.tmp, "tree", "lib"))
        shutil.copy(os.path.join(REPO, "lib", "ui.sh"), os.path.join(self.tmp, "tree", "lib", "ui.sh"))
        shutil.copytree(os.path.join(REPO, "deployment", "backup", "systemd"), os.path.join(self.mod, "systemd"))
        shutil.copy(os.path.join(REPO, "deployment", "backup", "setup-backup.sh"), self.mod)
        with open(os.path.join(self.mod, "synk-backup"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(self.mod, "synk-backup"), 0o755)
        self.root = os.path.join(self.tmp, "root")
        self.stub_dir = os.path.join(self.tmp, "stub")
        os.makedirs(self.stub_dir)
        self.script = os.path.join(self.mod, "setup-backup.sh")

    def run_setup(self, *args, env=None, bash=None):
        e = {k: v for k, v in os.environ.items() if not k.startswith("SOFTSYNK_")}
        e.update(STUB_DIR=self.stub_dir, STUB_DEFAULT_REPO=self.root + "/data/backup/restic")
        e.update(env or {})
        return subprocess.run([bash or BASHES[0], self.script, "--root", self.root, *args], capture_output=True,
                              text=True, env=e, stdin=subprocess.DEVNULL, timeout=60)

    def configure_call(self):
        p = os.path.join(self.stub_dir, "configure.json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    @staticmethod
    def opts(argv):
        """{--option: value-or-None} for everything after `configure`."""
        out, i = {}, 1
        while i < len(argv):
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt is not None and not nxt.startswith("--"):
                out[argv[i]], i = nxt, i + 2
            elif nxt == "" and argv[i] in ("--mail-to", "--exclude-databases"):
                out[argv[i]], i = "", i + 2
            else:
                out[argv[i]], i = None, i + 1
        return out

    def assertNoSecrets(self, r):
        for text in (r.stdout, r.stderr):
            self.assertNotIn(PW, text)
            self.assertNotIn(SEC, text)

    def test_help_and_unknown_options(self):
        r = self.run_setup("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("SOFTSYNK_ANSWER_REPO_PASSWORD", r.stdout)
        self.assertNotIn("--repo-password", r.stdout)
        self.assertNotIn("--s3-access-key-id", r.stdout)
        self.assertIn("SOFTSYNK_ANSWER_S3_ACCESS_KEY_ID", r.stdout)
        for bad in (["--repo-password", PW], ["--s3-secret-access-key", SEC], ["--s3-access-key-id", KID],
                    ["--nope"]):
            r = self.run_setup(*bad)
            self.assertEqual(r.returncode, 64, bad)
            self.assertNoSecrets(r)
        r = self.run_setup("--local-repo")
        self.assertEqual(r.returncode, 64)
        self.assertIsNone(self.configure_call())

    def test_dry_run_full_answers_and_secret_routing(self):
        answers = ["--dry-run", "--non-interactive", "--local-repo", "/srv/bk", "--remote-preset", "r2",
                   "--remote-id", "acct123", "--remote-bucket", "bkt", "--remote-prefix", "pfx",
                   "--mail-to", "ops@example.com",
                   "--retention-daily", "5", "--retention-weekly", "2", "--retention-twice-monthly", "3",
                   "--exclude-databases", "a,b"]
        env = {"SOFTSYNK_ANSWER_REPO_PASSWORD": PW, "SOFTSYNK_ANSWER_S3_ACCESS_KEY_ID": KID,
               "SOFTSYNK_ANSWER_S3_SECRET_ACCESS_KEY": SEC}
        for bash in BASHES:
            if os.path.exists(os.path.join(self.stub_dir, "configure.json")):
                os.unlink(os.path.join(self.stub_dir, "configure.json"))
            r = self.run_setup(*answers, env=env, bash=bash)
            self.assertEqual(r.returncode, 0, (bash, r.stderr))
            self.assertNoSecrets(r)
            call = self.configure_call()
            o = self.opts(call["argv"])
            self.assertEqual(o["--local-repo"], "/srv/bk")
            self.assertEqual(o["--remote-preset"], "r2")
            self.assertEqual((o["--remote-id"], o["--remote-bucket"], o["--remote-prefix"]), ("acct123", "bkt", "pfx"))
            self.assertEqual(o["--mail-to"], "ops@example.com")
            self.assertEqual((o["--retention-daily"], o["--retention-weekly"], o["--retention-twice-monthly"]),
                             ("5", "2", "3"))
            self.assertEqual(o["--exclude-databases"], "a,b")
            self.assertIn("--secrets-stdin", o)
            self.assertIn("--dry-run", o)
            self.assertNotIn(PW, json.dumps(call["argv"]))
            self.assertNotIn(SEC, json.dumps(call["argv"]))
            self.assertEqual(json.loads(call["stdin"]), {
                "restic_password": PW, "s3_access_key_id": KID, "s3_secret_access_key": SEC})
            self.assertNotIn(KID, json.dumps(call["argv"]))
            self.assertEqual(call["answer_env"], [], "answers must be consumed, not left in the environment")
        self.assertFalse(os.path.exists(os.path.join(self.root, "log")), "dry run must not write")

    def test_failure_mail_goes_through_the_shared_alert_unit(self):
        systemd = os.path.join(REPO, "deployment", "backup", "systemd")
        with open(os.path.join(systemd, "softsynk-backup.service.in")) as f:
            self.assertIn("OnFailure=softsynk-alert@%n.service", f.read())
        self.assertFalse(os.path.exists(os.path.join(systemd, "softsynk-notify@.service.in")))
        r = self.run_setup("--help")
        self.assertIn("ignored", r.stdout.split("--mail-to", 1)[1].splitlines()[0])

    def test_accept_defaults_without_remote(self):
        r = self.run_setup("--dry-run", "--accept-defaults", "--non-interactive",
                           env={"SOFTSYNK_ANSWER_REPO_PASSWORD": PW})
        self.assertEqual(r.returncode, 0, r.stderr)
        o = self.opts(self.configure_call()["argv"])
        self.assertEqual(o["--local-repo"], self.root + "/data/backup/restic")
        self.assertEqual(o["--remote-preset"], "none")
        self.assertNotIn("--remote-bucket", o)
        self.assertEqual(o["--mail-to"], "")
        self.assertEqual(o["--exclude-databases"], "")
        self.assertEqual((o["--retention-daily"], o["--retention-weekly"], o["--retention-twice-monthly"]),
                         ("3", "1", "2"))
        self.assertEqual(json.loads(self.configure_call()["stdin"]), {"restic_password": PW})
        self.assertIn("No off-server copy", r.stderr)

    def test_stored_password_and_keys_are_kept(self):
        stored = {"configured": True, "has_password": True, "has_s3_keys": True,
                  "defaults": {"local_repo": "x", "prefix": "softsynk-h"},
                  "config": {"local_repo": "/srv/old", "mail_to": "me@example.com", "exclude_databases": ["tmp"],
                             "retention": {"daily": 7, "weekly": 4, "twice_monthly": 6},
                             "remote": {"preset": "r2", "endpoint": "https://acct9.r2.cloudflarestorage.com",
                                        "region": "auto", "bucket": "old-bkt", "prefix": "old-pfx"}}}
        r = self.run_setup("--dry-run", "--accept-defaults", "--non-interactive", env={"STUB_CONFIG": json.dumps(stored)})
        self.assertEqual(r.returncode, 0, r.stderr)
        call = self.configure_call()
        o = self.opts(call["argv"])
        self.assertEqual((o["--local-repo"], o["--mail-to"], o["--exclude-databases"]),
                         ("/srv/old", "", "tmp"), "a stored mail_to is ignored now, so a re-run drops it")
        self.assertEqual((o["--remote-preset"], o["--remote-id"], o["--remote-bucket"], o["--remote-prefix"]),
                         ("r2", "acct9", "old-bkt", "old-pfx"))
        self.assertEqual(o["--retention-daily"], "7")
        self.assertEqual(json.loads(call["stdin"]), {}, "nothing secret is re-sent when the stored ones are kept")

    def test_missing_input_exits_64_and_configures_nothing(self):
        cases = [
            (["--dry-run", "--non-interactive"], {}),  # no answers, no --accept-defaults
            (["--dry-run", "--accept-defaults", "--non-interactive"], {}),  # no password
            (["--dry-run", "--accept-defaults", "--non-interactive", "--remote-preset", "b2", "--remote-id", "us-west-004",
              "--remote-bucket", "b", "--remote-prefix", "p"],
             {"SOFTSYNK_ANSWER_REPO_PASSWORD": PW, "SOFTSYNK_ANSWER_S3_ACCESS_KEY_ID": KID}),  # no S3 secret
            (["--dry-run", "--accept-defaults", "--non-interactive", "--remote-preset", "custom"],
             {"SOFTSYNK_ANSWER_REPO_PASSWORD": PW}),  # custom without endpoint
            (["--dry-run", "--accept-defaults", "--non-interactive"], {"SOFTSYNK_ANSWER_REPO_PASSWORD": "short"}),
        ]
        for args, env in cases:
            r = self.run_setup(*args, env=env)
            self.assertEqual(r.returncode, 64, (args, r.stderr))
            self.assertIsNone(self.configure_call(), args)
            self.assertNoSecrets(r)
        r = self.run_setup("--dry-run", "--accept-defaults", "--non-interactive")
        self.assertIn("SOFTSYNK_ANSWER_REPO_PASSWORD", r.stderr)

    def test_bad_remote_preset_value(self):
        r = self.run_setup("--dry-run", "--accept-defaults", "--non-interactive", "--remote-preset", "gcs",
                           env={"SOFTSYNK_ANSWER_REPO_PASSWORD": PW})
        self.assertEqual(r.returncode, 64)

    def test_configure_failure_stops_before_anything_is_installed(self):
        r = self.run_setup("--dry-run", "--accept-defaults", "--non-interactive",
                           env={"SOFTSYNK_ANSWER_REPO_PASSWORD": PW, "STUB_CONFIGURE_RC": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Configuration failed", r.stderr)
        self.assertNotIn("Dry run finished", r.stderr)

    def test_odd_root_is_refused(self):
        e = dict(os.environ, STUB_DIR=self.stub_dir)
        r = subprocess.run([BASHES[0], self.script, "--root", "/tmp/has space", "--dry-run"], capture_output=True,
                           text=True, env=e, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(r.returncode, 64)

    @unittest.skipIf(os.geteuid() == 0, "needs a non-root user")
    def test_real_run_refuses_without_root(self):
        r = self.run_setup("--accept-defaults", "--non-interactive", env={"SOFTSYNK_ANSWER_REPO_PASSWORD": PW})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Run as root", r.stderr)
        self.assertIsNone(self.configure_call())
        # only the session log may exist; no config, secrets, repository or state
        found = set(os.listdir(self.root)) if os.path.exists(self.root) else set()
        self.assertLessEqual(found, {"log"})


if __name__ == "__main__":
    unittest.main()
