"""Actual ASGI route tests with an injected database, not PostgreSQL proof."""

import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

from folio import workbench

WORKSPACE = workbench.WORKSPACE
CASE = "22222222-2222-4222-8222-222222222222"
SOURCE = "33333333-3333-4333-8333-333333333333"


class OriginalDownloadRoute(unittest.TestCase):
    def test_authentication_scope_live_bytes_and_deleted_tombstone(self):
        raw = b"Authored route document"
        sha = hashlib.sha256(raw).hexdigest()
        source = {"id": SOURCE, "sha256": sha, "name": "invoice.csv"}
        scenarios = [
            (True, WORKSPACE, raw, None, 200),
            (False, WORKSPACE, raw, None, 401),
            (True, "44444444-4444-4444-8444-444444444444", raw, None, 404),
            (True, WORKSPACE, None, "deleted", 410),
        ]
        for (
            authenticated,
            workspace,
            stored_raw,
            deleted_at,
            expected_status,
        ) in scenarios:
            with self.subTest(
                authenticated=authenticated, workspace=workspace, status=expected_status
            ):
                db = MagicMock()
                db.__enter__.return_value = db
                db.__exit__.return_value = False

                def execute(sql, params):
                    result = MagicMock()
                    if "FROM sessions" in sql:
                        # Viewers may not export originals; see test_original_availability.
                        result.fetchone.return_value = (
                            ("reviewer", ["reviewer"], "unit-csrf")
                            if authenticated
                            else None
                        )
                    elif "FROM originals" in sql:
                        self.assertEqual(params, (WORKSPACE, CASE, SOURCE))
                        result.fetchone.return_value = (
                            stored_raw,
                            sha,
                            deleted_at,
                            [source],
                            len(raw) if stored_raw is not None else None,
                        )
                    else:
                        self.fail(
                            "Unexpected database operation in read-only original download"
                        )
                    return result

                db.execute.side_effect = execute
                with (
                    patch.object(workbench, "connect", return_value=db),
                    patch.object(workbench, "pooled", return_value=db),
                ):
                    client = TestClient(workbench.app)
                    try:
                        client.cookies.set("folio_session", "unit-only-session")
                        response = client.get(
                            f"/api/v1/workspaces/{workspace}/cases/{CASE}/sources/{SOURCE}/original"
                        )
                    finally:
                        client.close()
                self.assertEqual(response.status_code, expected_status, response.text)
                self.assertEqual(response.headers.get("cache-control"), "no-store")
                self.assertEqual(
                    response.headers.get("x-content-type-options"), "nosniff"
                )
                if expected_status == 200:
                    self.assertEqual(response.content, raw)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertEqual(
                        response.headers["x-content-type-options"], "nosniff"
                    )
                    self.assertTrue(
                        response.headers["content-disposition"].startswith(
                            "attachment;"
                        )
                    )
                elif expected_status == 410:
                    self.assertEqual(response.json()["detail"], "ORIGINAL_DELETED")
                if expected_status in (401, 404):
                    self.assertFalse(
                        any(
                            "FROM originals" in call.args[0]
                            for call in db.execute.call_args_list
                        )
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
