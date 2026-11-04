"""Manifest and registry validation, dependency order, cycles, blocking."""
import json
import os
import unittest

import support
from support import Base, engine

ENGINE = support.load_engine()

# fixture -> text that must appear in the validation error
BAD = {
    "bad-shell-string": "must be an argv array, not a shell string",
    "bad-abs-path": "absolute path not allowed",
    "bad-dotdot": "'..' not allowed",
    "bad-interface": "unsupported interface 2",
    "bad-schema": "unsupported or missing manifest schema 2",
    "bad-missing-field": "missing or empty required field 'description'",
    "bad-no-status": "operations.status is required",
    "bad-missing-entry": "file not found: nope.sh",
    "bad-unknown-dep": "requires unknown module 'nonexistent'",
    "bad-unknown-field": "unknown field 'privileges'",
    "bad-id": "does not match registry id",
    "bad-toml": "cannot read manifest",
}


class ManifestValidation(Base):
    def validate(self, mods, **kw):
        rel = self.release(mods, **kw)
        return rel, engine(rel, ["validate"], self.root)

    def test_every_bad_fixture_is_rejected_with_a_clear_error(self):
        for fixture, message in BAD.items():
            with self.subTest(fixture=fixture):
                _, r = self.validate([fixture], name=fixture)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(f"INVALID {fixture}", r.stdout)
                self.assertIn(message, r.stdout)

    def test_cycle_is_detected_and_both_members_rejected(self):
        _, r = self.validate(["cycle-a", "cycle-b"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("dependency cycle: cycle-a -> cycle-b -> cycle-a", r.stdout)
        self.assertIn("INVALID cycle-a", r.stdout)
        self.assertIn("INVALID cycle-b", r.stdout)

    def test_after_cycle_is_also_a_cycle(self):
        rel = self.release(["alpha", "zeta"])
        zeta = rel / "deployment" / "alpha" / "module.toml"
        zeta.write_text(zeta.read_text().replace("requires = []", 'requires = []\nafter = ["zeta"]'))
        r = engine(rel, ["validate"], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("dependency cycle", r.stdout)

    def test_not_executable_program_is_rejected(self):
        rel = self.release(["alpha"])
        run = rel / "deployment" / "alpha" / "run.sh"
        run.chmod(0o644)
        r = engine(rel, ["validate"], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("file is not executable: run.sh", r.stdout)

    def test_symlink_escape_is_rejected(self):
        rel = self.release(["alpha"])
        outside = self.tmp / "outside"
        outside.mkdir()
        script = outside / "evil.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        link = rel / "deployment" / "alpha" / "escape"
        link.symlink_to(outside)
        toml = rel / "deployment" / "alpha" / "module.toml"
        toml.write_text(toml.read_text().replace('["run.sh", "install", "ok"]', '["escape/evil.sh"]'))
        r = engine(rel, ["validate"], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("escapes the release directory", r.stdout)

    def test_manifest_path_outside_release_is_rejected(self):
        rel = self.release([], registry='schema = 1\n[[modules]]\nid = "evil"\nmanifest = "../x/module.toml"\n')
        r = engine(rel, ["validate"], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("'..' not allowed", r.stdout)

    def test_good_fixtures_validate(self):
        rel, r = self.validate(["alpha", "beta", "gamma", "slowmod", "ubuntuonly", "zeta"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("6 valid, 0 invalid", r.stdout)

    def test_one_bad_module_does_not_break_the_others(self):
        rel = self.release(["alpha", "bad-shell-string", "needs-bad"])
        support.mark_installed(self.root, "alpha")
        r = engine(rel, ["--status", "--json"], self.root)
        doc = json.loads(r.stdout)
        by_id = {m["module"]: m for m in doc["modules"]}
        self.assertEqual(by_id["alpha"]["health"], "healthy")
        self.assertEqual(by_id["bad-shell-string"]["health"], "unknown")
        self.assertIn("shell string", " ".join(by_id["bad-shell-string"]["warnings"]))
        self.assertEqual(r.returncode, 3)

    def test_registry_level_errors_are_fatal_with_a_message(self):
        cases = {
            "schema = 2\n": "unsupported or missing schema",
            "schema = 1\nextra = 1\n": "unknown field",
            'schema = 1\n[[modules]]\nid = "Bad_ID"\nmanifest = "x"\n': "invalid module id",
            'schema = 1\n[[modules]]\nid = "alpha"\n': "needs exactly 'id' and 'manifest'",
            'schema = 1\n[[modules]]\nid = "dupe"\nmanifest = "a"\n[[modules]]\nid = "dupe"\nmanifest = "b"\n':
                "duplicate module id",
            "schema = \n": "cannot read registry",
        }
        for n, (text, message) in enumerate(cases.items()):
            with self.subTest(message=message):
                rel = self.release([], name=f"reg{n}", registry=text)
                r = engine(rel, ["validate"], self.root)
                self.assertEqual(r.returncode, 2)
                self.assertIn(message, r.stderr)

    def test_missing_registry_is_an_error(self):
        rel = self.release([])
        (rel / "modules.toml").unlink()
        r = engine(rel, ["validate"], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot read registry", r.stderr)

    def test_real_registry_lists_the_three_planned_modules(self):
        import tomllib
        with open(support.REPO / "modules.toml", "rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["schema"], 1)
        self.assertEqual([m["id"] for m in data["modules"]], ["backup", "redis", "auto-updates"])


class Ordering(Base):
    def test_dependencies_come_first_registry_order_breaks_ties(self):
        # registry lists beta (requires alpha) and zeta (after alpha) before alpha
        rel = self.release(["beta", "zeta", "alpha"])
        reg = ENGINE.Registry(str(rel))
        self.assertEqual(reg.order, ["alpha", "beta", "zeta"])

    def test_independent_modules_keep_registry_order(self):
        rel = self.release(["gamma", "alpha"])
        self.assertEqual(ENGINE.Registry(str(rel)).order, ["gamma", "alpha"])

    def test_unusable_propagates_through_requires(self):
        reg = ENGINE.Registry(str(self.release(["bad-shell-string", "needs-bad"])))
        self.assertIn("requires bad-shell-string", reg.unusable("needs-bad"))
        self.assertIsNone(ENGINE.Registry(str(self.release(["alpha", "beta"], name="ok"))).unusable("beta"))

    def test_platform_gate(self):
        rel = self.release(["ubuntuonly"])
        for key, unsupported in (("ubuntu-24.04", False), ("ubuntu-22.04", True), ("debian-12", True)):
            with self.subTest(os=key):
                os.environ["SOFTSYNK_OS_RELEASE"] = support.OS_RELEASE[key]
                try:
                    reg = ENGINE.Registry(str(rel))
                finally:
                    del os.environ["SOFTSYNK_OS_RELEASE"]
                self.assertEqual(bool(reg.entries["ubuntuonly"].unsupported), unsupported)


class Helpers(unittest.TestCase):
    def test_resolve_inside(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            rel = Path(d).resolve()
            (rel / "a").mkdir()
            (rel / "a" / "f").write_text("x")
            self.assertEqual(ENGINE.resolve_inside(str(rel / "a"), "f", str(rel)), str(rel / "a" / "f"))
            for bad in ("/etc/passwd", "../x", "a/../../x", "", "x\0y"):
                with self.subTest(path=bad), self.assertRaises(ValueError):
                    ENGINE.resolve_inside(str(rel / "a"), bad, str(rel))
            (rel / "a" / "out").symlink_to("/")
            with self.assertRaises(ValueError):
                ENGINE.resolve_inside(str(rel / "a"), "out/etc", str(rel))

    def test_atomic_write_replaces_whole_file_and_leaves_no_temp(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "f"
            ENGINE.atomic_write(str(target), "one\n")
            ENGINE.atomic_write(str(target), "two\n", 0o600)
            self.assertEqual(target.read_text(), "two\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual([p.name for p in Path(d).iterdir()], ["f"])


if __name__ == "__main__":
    unittest.main()
