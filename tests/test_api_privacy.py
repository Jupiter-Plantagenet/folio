"""In-process HTTP privacy tests; no retained database or lifespan execution."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

from folio import workbench


class PrivateApiResponses(unittest.TestCase):
    def test_private_routes_and_errors_are_never_cacheable(self):
        db = MagicMock()
        db.__enter__.return_value = db
        db.__exit__.return_value = False
        db.execute.return_value.fetchone.return_value = None
        paths = [
            (f"/api/v1/workspaces/{workbench.WORKSPACE}/cases", 401),
            ("/api/v1/auth/session", 401),
            ("/api/v1/no-such-route", 404),
        ]
        with (
            patch.object(workbench, "connect", return_value=db),
            patch.object(workbench, "pooled", return_value=db),
        ):
            client = TestClient(workbench.app)
            try:
                for path, status in paths:
                    with self.subTest(path=path):
                        response = client.get(path)
                        self.assertEqual(response.status_code, status)
                        self.assertEqual(
                            response.headers.get("cache-control"), "no-store"
                        )
                        self.assertEqual(response.headers.get("pragma"), "no-cache")
                        self.assertEqual(
                            response.headers.get("x-content-type-options"), "nosniff"
                        )
                        self.assertEqual(
                            response.headers.get("referrer-policy"), "no-referrer"
                        )
            finally:
                client.close()

    def test_unexpected_private_errors_are_redacted_and_not_cacheable(self):
        with (
            patch.object(
                workbench, "connect", side_effect=RuntimeError("PRIVATE_SENTINEL")
            ),
            patch.object(
                workbench, "pooled", side_effect=RuntimeError("PRIVATE_SENTINEL")
            ),
        ):
            client = TestClient(workbench.app, raise_server_exceptions=False)
            try:
                response = client.get(f"/api/v1/workspaces/{workbench.WORKSPACE}/cases")
            finally:
                client.close()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        self.assertNotIn("PRIVATE_SENTINEL", response.text)
        self.assertEqual(response.json()["detail"], "INTERNAL_ERROR")


if __name__ == "__main__":
    unittest.main(verbosity=2)
