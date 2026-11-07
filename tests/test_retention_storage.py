"""Focused SQL-adapter tests; not PostgreSQL execution or deletion evidence."""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock


class OriginalExpiryScope(unittest.TestCase):
    def test_scheduled_expiry_is_age_scoped_but_explicit_deletion_is_not(self):
        source = Path(__file__).resolve().parents[1] / "folio" / "retention_storage.py"
        self.assertTrue(source.is_file(), "AGE_SCOPED_ORIGINAL_DELETION_MISSING")
        spec = importlib.util.spec_from_file_location("retention_storage", source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for automatic in (True, False):
            with self.subTest(automatic=automatic):
                db = Mock()
                expected = [("old-source", "a" * 64)]
                db.execute.return_value.fetchall.return_value = expected
                result = module.delete_original_rows(
                    db, "workspace", "case", expired_only=automatic
                )
                query, parameters = db.execute.call_args.args
                self.assertEqual(parameters, ("workspace", "case"))
                self.assertIn("workspace=%s AND case_id=%s AND raw IS NOT NULL", query)
                self.assertEqual(
                    "created_at < clock_timestamp()-interval '90 days'" in query,
                    automatic,
                )
                self.assertTrue(query.endswith("RETURNING id,sha256"))
                db.execute.assert_called_once()
                self.assertEqual(result, expected)
        db = Mock()
        with self.assertRaisesRegex(ValueError, "^EXPIRY_MODE_REQUIRED$"):
            module.delete_original_rows(db, "workspace", "case", expired_only="true")
        db.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
