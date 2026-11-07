"""Safety contract for the local compiled-UI/API launcher; no services or DB."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LocalRuntime(unittest.TestCase):
    def test_only_existing_database_principals_runtime_mode_and_owned_ui_are_used(self):
        source = ROOT / "serve_local.py"
        self.assertTrue(source.is_file(), "SAFE_LOCAL_RUNTIME_LAUNCHER_MISSING")
        spec = importlib.util.spec_from_file_location("serve_local", source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        profile = {
            "FOLIO_DSN": "unit-workbench",
            "FOLIO_TARGET_DSN": "unit-target",
            "FOLIO_TARGET_READ_DSN": "unit-reader",
        }
        existing = {
            **profile,
            "FOLIO_SCHEMA_MODE": "runtime",
            "FOLIO_EXPORT_ROOT": "unit-exports",
            "UNRELATED_SECRET": "do-not-copy",
        }
        env = module.runtime_environment(profile, existing, 8111)
        self.assertEqual(env["FOLIO_SCHEMA_MODE"], "runtime")
        self.assertEqual(env["FOLIO_UI_ORIGIN"], "http://127.0.0.1:8111")
        self.assertEqual(env["FOLIO_EXPORT_ROOT"], "unit-exports")
        self.assertNotIn("UNRELATED_SECRET", env)
        self.assertNotIn("FOLIO_MIGRATION_DSN", env)
        for key in profile:
            self.assertEqual(env[key], profile[key])
            with self.subTest(changed_principal=key):
                with self.assertRaisesRegex(
                    RuntimeError, "^EXISTING_DATABASE_PRINCIPALS_REQUIRED$"
                ):
                    module.runtime_environment(
                        profile, {**existing, key: "different"}, 8111
                    )
        with self.assertRaisesRegex(RuntimeError, "^EXISTING_RUNTIME_MODE_REQUIRED$"):
            module.runtime_environment(
                profile, {**existing, "FOLIO_SCHEMA_MODE": "owner"}, 8111
            )
        with self.assertRaisesRegex(RuntimeError, "^DATABASE_PROFILE_REQUIRED$"):
            module.runtime_environment({}, existing, 8111)
        cache = ROOT / "web" / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="application-build-test-", dir=cache
        ) as folder:
            ui = Path(folder)
            (ui / "index.html").write_text(
                "<p>Authored UI fixture</p>", encoding="utf-8"
            )
            self.assertEqual(module.ui_directory(str(ui)), ui.resolve())
        with self.assertRaisesRegex(RuntimeError, "^OWNED_UI_BUILD_REQUIRED$"):
            module.ui_directory(str(ROOT / "web"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
