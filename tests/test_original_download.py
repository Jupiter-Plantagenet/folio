"""Ordinary tests for the document download storage adapter; no live DB access."""

import hashlib
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock

WORKSPACE = "11111111-1111-4111-8111-111111111111"
CASE = "22222222-2222-4222-8222-222222222222"
SOURCE = "33333333-3333-4333-8333-333333333333"


class OriginalDownload(unittest.TestCase):
    def test_only_matching_live_original_bytes_can_be_returned(self):
        path = Path(__file__).resolve().parents[1] / "folio" / "original_download.py"
        self.assertTrue(path.is_file(), "AUTHENTICATED_ORIGINAL_DOWNLOAD_MISSING")
        spec = importlib.util.spec_from_file_location("original_download", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        raw = b"Authored document bytes"
        sha = hashlib.sha256(raw).hexdigest()
        source = {
            "id": SOURCE,
            "sha256": sha,
            "name": "../invoice\r\nInjected: true.csv",
        }
        db = Mock()
        db.execute.return_value.fetchone.return_value = (
            raw,
            sha,
            None,
            [source],
            len(raw),
        )
        data, headers = module.read_original(db, WORKSPACE, CASE, SOURCE)
        self.assertEqual(data, raw)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        self.assertNotIn("\r", headers["Content-Disposition"])
        self.assertNotIn("\n", headers["Content-Disposition"])
        query, params = db.execute.call_args.args
        self.assertEqual(params, (WORKSPACE, CASE, SOURCE))
        self.assertIn("o.workspace=%s AND o.case_id=%s AND o.id=%s", query)
        self.assertIn("c.workspace=o.workspace AND c.id=o.case_id", query)
        self.assertIn("octet_length(o.raw)", query)
        rows = [
            (None, "NOT_FOUND", 404),
            ((None, sha, "deleted", [source], None), "ORIGINAL_DELETED", 410),
            (
                (raw, sha, None, [{**source, "original_deleted": True}], len(raw)),
                "ORIGINAL_DELETED",
                410,
            ),
            ((raw, sha, None, [], len(raw)), "ORIGINAL_METADATA_MISMATCH", 503),
            (
                (raw, sha, None, [{**source, "sha256": "0" * 64}], len(raw)),
                "ORIGINAL_DIGEST_MISMATCH",
                503,
            ),
            ((b"changed", sha, None, [source], 7), "ORIGINAL_DIGEST_MISMATCH", 503),
            ((None, sha, None, [source], 20971521), "ORIGINAL_TOO_LARGE", 413),
        ]
        for row, code, status in rows:
            with self.subTest(code=code, row_kind=type(row).__name__):
                db.execute.return_value.fetchone.return_value = row
                with self.assertRaises(module.OriginalUnavailable) as caught:
                    module.read_original(db, WORKSPACE, CASE, SOURCE)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.status, status)
        invalid = Mock()
        with self.assertRaises(module.OriginalUnavailable) as caught:
            module.read_original(invalid, WORKSPACE, CASE, "../source")
        self.assertEqual(caught.exception.status, 404)
        invalid.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
