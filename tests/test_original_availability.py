"""Per-source original availability through the actual ASGI detail/download routes.

The database is injected (not PostgreSQL proof). The fake emulates the stored
`originals` rows and computes the PostgreSQL-side digest with hashlib, so each
scenario can be checked against the real download route in lockstep.
"""

import copy
import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402

from folio import original_download, workbench  # noqa: E402

WORKSPACE = workbench.WORKSPACE
OTHER_WORKSPACE = "44444444-4444-4444-8444-444444444444"
CASE = "22222222-2222-4222-8222-222222222222"
MAX = original_download.MAX_ORIGINAL_BYTES
UPLOADED = "user-attested independently authored synthetic input"
SAMPLE = "Independently authored synthetic test assets by Folio implementation; not private documents"


def sid(n):
    return f"33333333-3333-4333-8333-{n:012d}"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


GOOD = b"Receipt,GR-A\nLine,1\nReceived quantity,40\nUnit,each\nCurrency,USD\n"
OTHER = b"different stored bytes"

# source id -> (source JSON, stored row or None). Stored rows hold the exact
# bytes, the recorded digest, deletion marker, optional oversize length, and
# whether the row predates the 90-day retention window.
SCENARIOS = {
    "sample_fixture_not_stored": (
        {"id": sid(1), "sha256": sha(b"sample"), "authorship": SAMPLE},
        None,
    ),
    "attached_original_available": (
        {"id": sid(2), "sha256": sha(GOOD), "authorship": UPLOADED},
        {"raw": GOOD, "sha": sha(GOOD), "deleted_at": None},
    ),
    "uploaded_object_missing": (
        {"id": sid(3), "sha256": sha(GOOD), "authorship": UPLOADED},
        None,
    ),
    "bytes_missing_without_tombstone": (
        {"id": sid(4), "sha256": sha(GOOD), "authorship": UPLOADED},
        {"raw": None, "sha": sha(GOOD), "deleted_at": None},
    ),
    "tombstoned_row": (
        {
            "id": sid(5),
            "sha256": sha(GOOD),
            "authorship": UPLOADED,
            "original_deleted": True,
        },
        {"raw": None, "sha": sha(GOOD), "deleted_at": "2026-10-01T00:00:00Z"},
    ),
    "source_tombstone_with_bytes": (
        {
            "id": sid(6),
            "sha256": sha(GOOD),
            "authorship": UPLOADED,
            "original_deleted": True,
        },
        {"raw": GOOD, "sha": sha(GOOD), "deleted_at": None},
    ),
    "metadata_digest_differs": (
        {"id": sid(7), "sha256": "0" * 64, "authorship": UPLOADED},
        {"raw": GOOD, "sha": sha(GOOD), "deleted_at": None},
    ),
    "stored_bytes_changed": (
        {"id": sid(8), "sha256": sha(GOOD), "authorship": UPLOADED},
        {"raw": OTHER, "sha": sha(GOOD), "deleted_at": None},
    ),
    "malformed_stored_digest": (
        {"id": sid(9), "sha256": "NOT-A-DIGEST", "authorship": UPLOADED},
        {"raw": GOOD, "sha": "NOT-A-DIGEST", "deleted_at": None},
    ),
    "too_large": (
        {"id": sid(10), "sha256": sha(GOOD), "authorship": UPLOADED},
        {"raw": GOOD, "sha": sha(GOOD), "deleted_at": None, "length": MAX + 1},
    ),
    "retention_expired_still_stored": (
        {"id": sid(11), "sha256": sha(GOOD), "authorship": UPLOADED},
        {"raw": GOOD, "sha": sha(GOOD), "deleted_at": None, "expired": True},
    ),
}

EXPECTED = {
    "sample_fixture_not_stored": ("not_stored", "NOT_FOUND", 404),
    "attached_original_available": ("available", None, 200),
    "uploaded_object_missing": ("missing", "NOT_FOUND", 404),
    "bytes_missing_without_tombstone": ("bytes_missing", "ORIGINAL_BYTES_MISSING", 503),
    "tombstoned_row": ("deleted", "ORIGINAL_DELETED", 410),
    "source_tombstone_with_bytes": ("deleted", "ORIGINAL_DELETED", 410),
    "metadata_digest_differs": ("digest_mismatch", "ORIGINAL_DIGEST_MISMATCH", 503),
    # Advisory availability is metadata-only; only the download re-hash sees bit rot.
    "stored_bytes_changed": ("available", "ORIGINAL_DIGEST_MISMATCH", 503),
    "malformed_stored_digest": ("digest_mismatch", "ORIGINAL_DIGEST_MISMATCH", 503),
    "too_large": ("too_large", "ORIGINAL_TOO_LARGE", 413),
    "retention_expired_still_stored": ("available", None, 200),
}


def sample_case(sources, *, legal_hold=False):
    # Sample-origin case: no intake marker, so availability must not depend on it.
    return {
        "id": CASE,
        "workspace": WORKSPACE,
        "version": 4,
        "state": "needs_review",
        "sample_digest": "fixture",
        "sources": sources,
        "source_digest": "f" * 64,
        "proposal": None,
        "retention": {
            "original_days": 90,
            "decision_evidence_days": 365,
            "legal_hold": legal_hold,
        },
    }


class FakeDatabase:
    """Injected adapter: answers only the read-only statements these routes use."""

    def __init__(self, case, stored, authenticated=True, roles=("reviewer",)):
        self.case = case
        self.stored = stored
        self.authenticated = authenticated
        self.roles = list(roles)
        self.statements = []
        self.mock = MagicMock()
        self.mock.__enter__.return_value = self.mock
        self.mock.__exit__.return_value = False
        self.mock.execute.side_effect = self.execute

    def result(self, rows):
        result = MagicMock()
        result.fetchone.return_value = rows[0] if rows else None
        result.fetchall.return_value = rows
        result.__iter__.return_value = iter(rows)
        return result

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "FROM sessions" in sql:
            return self.result(
                [("member", self.roles, "unit-csrf")] if self.authenticated else []
            )
        if sql.startswith("SELECT body FROM cases"):
            assert params == (WORKSPACE, CASE), params
            return self.result([(copy.deepcopy(self.case),)])
        if "FROM jobs" in sql or "FROM analysis_runs" in sql:
            return self.result([])
        if "FROM originals o JOIN cases c" in sql:
            # Download-time read: exact bytes up to the limit plus the case sources.
            workspace, case_id, source_id = params
            row = (
                self.stored.get(source_id)
                if (workspace, case_id) == (WORKSPACE, CASE)
                else None
            )
            if row is None:
                return self.result([])
            length = row.get("length", None if row["raw"] is None else len(row["raw"]))
            raw = row["raw"] if length is not None and length <= MAX else None
            return self.result(
                [
                    (
                        raw,
                        row["sha"],
                        row["deleted_at"],
                        copy.deepcopy(self.case["sources"]),
                        length,
                    )
                ]
            )
        if "FROM originals" in sql:
            # Availability metadata: neither the bytes nor a hash of them is read.
            assert params == (WORKSPACE, CASE), params
            assert "sha256(o.raw)" not in sql
            assert "SELECT o.raw" not in sql and ",o.raw," not in sql.replace(" ", "")
            rows = []
            for source_id, row in self.stored.items():
                length = row.get(
                    "length", None if row["raw"] is None else len(row["raw"])
                )
                rows.append(
                    (
                        source_id,
                        row["sha"],
                        row["deleted_at"],
                        length,
                        row.get("expired", False),
                    )
                )
            return self.result(rows)
        raise AssertionError("Unexpected database operation: " + sql)


def call(fake, path):
    with (
        patch.object(workbench, "connect", return_value=fake.mock),
        patch.object(workbench, "pooled", return_value=fake.mock),
    ):
        client = TestClient(workbench.app)
        try:
            client.cookies.set("folio_session", "unit-only-session")
            return client.get(path)
        finally:
            client.close()


def detail_path(workspace=WORKSPACE):
    return f"/api/v1/workspaces/{workspace}/cases/{CASE}"


class OriginalAvailabilityRoute(unittest.TestCase):
    def build(self, names=None, *, legal_hold=False, authenticated=True):
        names = names or list(SCENARIOS)
        sources = [copy.deepcopy(SCENARIOS[name][0]) for name in names]
        stored = {
            SCENARIOS[name][0]["id"]: SCENARIOS[name][1]
            for name in names
            if SCENARIOS[name][1] is not None
        }
        return FakeDatabase(
            sample_case(sources, legal_hold=legal_hold), stored, authenticated
        )

    def test_detail_reports_distinct_state_for_every_source_without_intake_marker(self):
        fake = self.build()
        response = call(fake, detail_path())
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotIn("intake_digest", body)
        availability = body["original_availability"]
        self.assertEqual(
            availability["schema_version"], "folio-original-availability-v1"
        )
        self.assertIs(availability["advisory"], True)
        self.assertIs(availability["legal_hold"], False)
        entries = {entry["source_id"]: entry for entry in availability["sources"]}
        self.assertEqual(len(entries), len(SCENARIOS))
        for name, (source, _row) in SCENARIOS.items():
            with self.subTest(name=name):
                entry = entries[source["id"]]
                state, code, _status = EXPECTED[name]
                self.assertEqual(entry["state"], state)
                # The code column is the download outcome; advisory reports none when
                # metadata looks available.
                self.assertEqual(entry["code"], None if state == "available" else code)
                self.assertIs(entry["downloadable"], state == "available")
                self.assertEqual(
                    set(entry),
                    {
                        "source_id",
                        "source_version",
                        "state",
                        "code",
                        "downloadable",
                        "bytes_stored",
                        "retention_expired",
                    },
                )
        self.assertIs(entries[sid(11)]["retention_expired"], True)
        self.assertIs(entries[sid(2)]["retention_expired"], False)
        self.assertIsNone(entries[sid(1)]["retention_expired"])
        self.assertIs(entries[sid(2)]["bytes_stored"], True)
        self.assertIs(entries[sid(4)]["bytes_stored"], False)
        self.assertIs(entries[sid(5)]["bytes_stored"], False)

    def test_availability_is_response_only_and_never_carries_bytes(self):
        fake = self.build()
        before = copy.deepcopy(fake.case)
        body = call(fake, detail_path()).json()
        self.assertFalse(
            any(
                not sql.lstrip().upper().startswith("SELECT")
                for sql, _ in fake.statements
            )
        )
        self.assertEqual(body["sources"], before["sources"])
        self.assertEqual(body["source_digest"], before["source_digest"])
        for source in body["sources"]:
            self.assertNotIn("availability", source)
        text = str(body)
        self.assertNotIn(GOOD.decode(), text)
        self.assertNotIn(OTHER.decode(), text)

    def test_download_route_agrees_with_advisory_state_and_rechecks(self):
        for name in SCENARIOS:
            with self.subTest(name=name):
                fake = self.build([name])
                entry = call(fake, detail_path()).json()["original_availability"][
                    "sources"
                ][0]
                source_id = SCENARIOS[name][0]["id"]
                response = call(fake, detail_path() + f"/sources/{source_id}/original")
                _state, code, status = EXPECTED[name]
                self.assertEqual(response.status_code, status, response.text)
                if status == 200:
                    self.assertTrue(entry["downloadable"])
                    self.assertEqual(response.content, SCENARIOS[name][1]["raw"])
                else:
                    # Never advertises a download the route would refuse, except bit
                    # rot under matching metadata, which only the re-hash can detect.
                    self.assertEqual(
                        entry["downloadable"], name == "stored_bytes_changed"
                    )
                    self.assertEqual(response.json()["detail"], code)

    def test_roles_without_original_export_are_denied_before_any_original_read(self):
        for roles in (["viewer"], ["auditor"], ["admin"]):
            with self.subTest(roles=roles):
                fake = FakeDatabase(
                    sample_case([SCENARIOS["attached_original_available"][0]]),
                    {sid(2): SCENARIOS["attached_original_available"][1]},
                    roles=roles,
                )
                availability = call(fake, detail_path()).json()["original_availability"]
                self.assertIs(availability["download_permitted"], False)
                self.assertEqual(availability["sources"][0]["state"], "available")
                self.assertIs(availability["sources"][0]["downloadable"], False)
                fake.statements.clear()
                response = call(fake, detail_path() + f"/sources/{sid(2)}/original")
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["detail"], "ORIGINAL_EXPORT_DENIED")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertFalse(any("originals" in sql for sql, _ in fake.statements))

    def test_reviewer_and_approver_may_download(self):
        for roles in (["reviewer"], ["approver"]):
            with self.subTest(roles=roles):
                fake = FakeDatabase(
                    sample_case([SCENARIOS["attached_original_available"][0]]),
                    {sid(2): SCENARIOS["attached_original_available"][1]},
                    roles=roles,
                )
                body = call(fake, detail_path()).json()["original_availability"]
                self.assertIs(body["download_permitted"], True)
                response = call(fake, detail_path() + f"/sources/{sid(2)}/original")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, GOOD)

    def test_download_rehashes_bytes_changed_after_availability_was_reported(self):
        fake = self.build(["attached_original_available"])
        entry = call(fake, detail_path()).json()["original_availability"]["sources"][0]
        self.assertTrue(entry["downloadable"])
        fake.stored[sid(2)] = {"raw": OTHER, "sha": sha(GOOD), "deleted_at": None}
        response = call(fake, detail_path() + f"/sources/{sid(2)}/original")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "ORIGINAL_DIGEST_MISMATCH")

    def test_legal_hold_is_reported_without_changing_byte_availability(self):
        fake = self.build(
            ["attached_original_available", "retention_expired_still_stored"],
            legal_hold=True,
        )
        availability = call(fake, detail_path()).json()["original_availability"]
        self.assertIs(availability["legal_hold"], True)
        self.assertEqual(
            [entry["state"] for entry in availability["sources"]],
            ["available", "available"],
        )

    def test_duplicate_source_identity_is_metadata_mismatch_for_both_entries(self):
        source = copy.deepcopy(SCENARIOS["attached_original_available"][0])
        fake = FakeDatabase(
            sample_case([source, {**source, "version": 2}]),
            {source["id"]: SCENARIOS["attached_original_available"][1]},
        )
        availability = call(fake, detail_path()).json()["original_availability"]
        self.assertEqual(
            [entry["state"] for entry in availability["sources"]],
            ["metadata_mismatch"] * 2,
        )
        self.assertEqual(
            [entry["source_version"] for entry in availability["sources"]], [None, 2]
        )
        response = call(fake, detail_path() + f"/sources/{source['id']}/original")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "ORIGINAL_METADATA_MISMATCH")

    def test_denials_match_download_route_and_never_query_originals(self):
        for authenticated, workspace, status in (
            (False, WORKSPACE, 401),
            (True, OTHER_WORKSPACE, 404),
        ):
            with self.subTest(authenticated=authenticated, workspace=workspace):
                fake = self.build(authenticated=authenticated)
                detail = call(fake, detail_path(workspace))
                download = call(
                    fake, detail_path(workspace) + f"/sources/{sid(2)}/original"
                )
                self.assertEqual(detail.status_code, status)
                self.assertEqual(download.status_code, status)
                self.assertEqual(detail.json()["detail"], download.json()["detail"])
                self.assertNotIn("original_availability", detail.text)
                self.assertFalse(any("originals" in sql for sql, _ in fake.statements))


class OriginalProblemClassifier(unittest.TestCase):
    def test_bytes_missing_without_tombstone_is_not_reported_as_deleted(self):
        source = {"id": sid(4), "sha256": sha(GOOD)}
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = (
            None,
            sha(GOOD),
            None,
            [source],
            None,
        )
        with self.assertRaises(original_download.OriginalUnavailable) as caught:
            original_download.read_original(db, WORKSPACE, CASE, sid(4))
        self.assertEqual(
            (caught.exception.code, caught.exception.status),
            ("ORIGINAL_BYTES_MISSING", 503),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
