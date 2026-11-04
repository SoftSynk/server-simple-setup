"""Status aggregation: schema, exit codes, timeout, read-only guarantee."""
import os
import time

import support
from support import Base, engine, mark_installed

MODULE_KEYS = {"module", "name", "version", "observed_at", "install_state", "health", "summary",
               "details", "warnings", "actions"}


class StatusJson(Base):
    def test_schema_versioned_and_exit_matches_overall(self):
        rel = self.release(["alpha"])
        mark_installed(self.root, "alpha")
        code, doc, _ = self.status_json(rel)
        self.assertEqual(code, 0)
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["framework"], {"version": "0.1.0", "interface": 1})
        self.assertEqual(doc["overall"], {"health": "healthy", "exit_code": 0})
        (m,) = doc["modules"]
        self.assertEqual(set(m), MODULE_KEYS)
        self.assertEqual((m["module"], m["install_state"], m["health"]), ("alpha", "installed", "healthy"))

    def test_exit_is_the_worst_health(self):
        rel = self.release(["alpha", "beta", "gamma"])
        mark_installed(self.root, "alpha")
        self.assertEqual(self.status_json(rel)[0], 0)
        mark_installed(self.root, "beta")
        code, doc, _ = self.status_json(rel)
        self.assertEqual((code, doc["overall"]["health"]), (1, "degraded"))
        mark_installed(self.root, "gamma")
        code, doc, _ = self.status_json(rel)
        self.assertEqual((code, doc["overall"]["health"]), (2, "unhealthy"))
        self.assertEqual(doc["overall"]["exit_code"], 2)

    def test_not_installed_modules_do_not_affect_exit(self):
        rel = self.release(["alpha", "gamma"])  # gamma reports unhealthy but is not installed
        mark_installed(self.root, "alpha")
        code, doc, _ = self.status_json(rel)
        self.assertEqual(code, 0)
        self.assertEqual({m["module"]: m["install_state"] for m in doc["modules"]},
                         {"alpha": "installed", "gamma": "not_installed"})

    def test_timeout_becomes_unknown_and_does_not_hang(self):
        rel = self.release(["slowmod", "alpha"])
        mark_installed(self.root, "slowmod", "alpha")
        t0 = time.time()
        code, doc, _ = self.status_json(rel, {"SOFTSYNK_STATUS_TIMEOUT": "3"})
        self.assertLess(time.time() - t0, 20)
        by_id = {m["module"]: m for m in doc["modules"]}
        self.assertEqual(by_id["slowmod"]["health"], "unknown")
        self.assertIn("timed out", " ".join(by_id["slowmod"]["warnings"]))
        self.assertEqual(by_id["alpha"]["health"], "healthy")  # later modules still run
        self.assertEqual(code, 3)
        pid = int((self.root / "slow.pid").read_text())
        with self.assertRaises(ProcessLookupError):  # the stuck child was killed, not left behind
            os.kill(pid, 0)

    def test_known_failure_outranks_unknown(self):
        rel = self.release(["slowmod", "gamma"])
        mark_installed(self.root, "slowmod", "gamma")
        code, doc, _ = self.status_json(rel, {"SOFTSYNK_STATUS_TIMEOUT": "3"})
        self.assertEqual((code, doc["overall"]["health"]), (2, "unhealthy"))

    def test_bad_status_output_is_unknown_with_a_warning(self):
        rel = self.release(["statusmode"])
        mark_installed(self.root, "statusmode")
        expected = {"badjson": "not valid JSON", "empty": "not valid JSON",
                    "wrongmodule": "expected 'statusmode'", "badschema": "unsupported status schema",
                    "badhealth": "invalid health"}
        for mode, message in expected.items():
            with self.subTest(mode=mode):
                code, doc, _ = self.status_json(rel, {"SOFTSYNK_TEST_STATUS": mode})
                (m,) = doc["modules"]
                self.assertEqual((code, m["health"]), (3, "unknown"))
                self.assertIn(message, " ".join(m["warnings"]))

    def test_unsupported_platform_is_reported_and_excluded(self):
        rel = self.release(["ubuntuonly"])
        mark_installed(self.root, "ubuntuonly")
        for key, health, code in (("debian-12", "unsupported", 0), ("ubuntu-22.04", "unsupported", 0),
                                  ("ubuntu-24.04", "healthy", 0)):
            with self.subTest(os=key):
                c, doc, _ = self.status_json(rel, {"SOFTSYNK_OS_RELEASE": support.OS_RELEASE[key]})
                self.assertEqual((c, doc["modules"][0]["health"]), (code, health))

    def test_nonzero_exit_with_valid_json_is_trusted(self):
        rel = self.release(["gamma"])  # fixture exits 2 after printing valid JSON
        mark_installed(self.root, "gamma")
        _, doc, _ = self.status_json(rel)
        self.assertEqual(doc["modules"][0]["health"], "unhealthy")


class StatusText(Base):
    def test_table_and_overall_line(self):
        rel = self.release(["alpha", "beta"])
        mark_installed(self.root, "alpha", "beta")
        r = engine(rel, ["--status"], self.root)
        self.assertEqual(r.returncode, 1)
        for text in ("MODULE", "alpha", "beta", "degraded", "! beta: fixture warning", "overall: degraded (exit 1)"):
            self.assertIn(text, r.stdout)

    def test_list_shows_unmet_requirement(self):
        rel = self.release(["alpha", "beta", "bad-shell-string"])
        r = engine(rel, ["--list"], self.root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("requires alpha (not installed)", r.stdout)
        self.assertIn("invalid manifest", r.stdout)

    def test_version_and_help(self):
        rel = self.release(["alpha"])
        r = engine(rel, ["--version"], self.root)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "softsynk-server 0.1.0 (module interface 1)"))
        r = engine(rel, ["--help"], self.root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("exit codes", r.stdout)
        self.assertIn("--rollback", r.stdout)


class ReadOnly(Base):
    def test_read_only_commands_write_nothing(self):
        rel = self.release(["alpha", "beta", "ubuntuonly", "bad-shell-string", "cmds"])
        self.root.mkdir()
        mark_installed(self.root, "alpha")
        before = (support.tree_hash(self.root), support.tree_hash(rel))
        for args in (["--status"], ["--status", "--json"], ["--list"], ["validate"], ["commands"], ["--version"], ["--help"]):
            with self.subTest(args=args):
                engine(rel, args, self.root)
                self.assertEqual((support.tree_hash(self.root), support.tree_hash(rel)), before)

    def test_read_only_commands_do_not_create_a_missing_root(self):
        rel = self.release(["alpha"])
        engine(rel, ["--status"], self.root)
        engine(rel, ["--list"], self.root)
        engine(rel, ["commands"], self.root)
        self.assertFalse(self.root.exists())

    def test_commands_lists_valid_manifest_commands_as_name_tab_path(self):
        rel = self.release(["alpha", "cmds", "bad-shell-string"])
        r = engine(rel, ["commands"], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [f"synk-one\t{rel}/deployment/cmds/one",
                                                 f"synk-two\t{rel}/deployment/cmds/sub/two"])


if __name__ == "__main__":
    import unittest
    unittest.main()
