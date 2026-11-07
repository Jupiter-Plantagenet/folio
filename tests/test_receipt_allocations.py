"""ADR-003: authored bytes through real parsers; no model/oracle shortcut."""

import io
from fractions import Fraction
from uuid import uuid4

from folio.formats import parse_document
from folio.provenance import extract_packet
from folio.receipts import receipt_manifest


def parsed(name, raw, role):
    return {
        **parse_document(raw, name),
        "id": str(uuid4()),
        "version": 1,
        "name": name,
        "document_role": role,
    }


def receipt(name, number, quantity, unit="each", currency="USD", line="1"):
    rows = [
        ["Receipt", number],
        ["Line", line],
        ["Received quantity", quantity],
        ["Unit", unit],
        ["Currency", currency],
    ]
    if name.endswith("xlsx"):
        from openpyxl import Workbook

        book = Workbook()
        for row in rows:
            book.active.append(row)
        out = io.BytesIO()
        book.save(out)
        raw = out.getvalue()
    else:
        raw = ("\n".join(",".join(row) for row in rows) + "\n").encode()
    return parsed(name, raw, "receipt")


def test_real_csv_xlsx_receipts_sum_exactly_and_aliases_never_count_twice():
    one = receipt("one.csv", "GR-A", "40.1")
    two = receipt("two.xlsx", "GR-B", "59.9")
    alias = receipt("alias.xlsx", "GR-A", "40.1")
    result = extract_packet([one, two, alias])
    assert result["values"]["received_quantity"] == "100"
    manifest = result["receipt_manifest"]
    assert len(manifest["rows"]) == 2
    assert sum(Fraction(r["quantity"]) for r in manifest["rows"]) == 100
    assert len(manifest["rows"][0]["evidence"]) == 2
    for row in manifest["rows"]:
        for evidence in row["evidence"]:
            source = next(
                s for s in [one, two, alias] if s["id"] == evidence["source_id"]
            )
            assert (
                source["pages"][0]["text"][evidence["start"] : evidence["end"]]
                == row["quantity"]
            )
            assert evidence["source_version"] == 1


def packet_files():
    import base64
    from pathlib import Path

    files = []
    for name, role in [
        ("invoice.pdf", "invoice"),
        ("purchase-order.pdf", "purchase_order"),
    ]:
        files.append(
            {
                "name": name,
                "document_role": role,
                "base64": base64.b64encode(
                    (
                        Path(__file__).resolve().parents[1] / "fixtures/public" / name
                    ).read_bytes()
                ).decode(),
            }
        )
    return files


def receipt_file(number, quantity, xlsx=False, currency="USD"):
    import base64

    if xlsx:
        from openpyxl import Workbook

        book = Workbook()
        rows = [
            ["Receipt", number],
            ["Line", "1"],
            ["Received quantity", quantity],
            ["Unit", "each"],
        ]
        if currency is not None:
            rows.append(["Currency", currency])
        for row in rows:
            book.active.append(row)
        out = io.BytesIO()
        book.save(out)
        return {
            "name": number + ".xlsx",
            "document_role": "receipt",
            "base64": base64.b64encode(out.getvalue()).decode(),
        }
    currency_line = f"Currency,{currency}\n" if currency is not None else ""
    return {
        "name": number + ".csv",
        "document_role": "receipt",
        "base64": base64.b64encode(
            f"Receipt,{number}\nLine,1\nReceived quantity,{quantity}\nUnit,each\n{currency_line}".encode()
        ).decode(),
    }


def test_missing_receipt_resumes_review_then_exact_two_receipt_effect_and_no_reuse():
    from fastapi.testclient import TestClient
    from test_domain_completion import session

    from folio.target import seed
    from folio.workbench import WORKSPACE, app, connect, migrate
    from folio.worker import claim, execute

    migrate()
    client = TestClient(app)
    h = session(client)
    prefix = f"/api/v1/workspaces/{WORKSPACE}"

    def headers(version=None):
        return {
            **h,
            "Idempotency-Key": str(uuid4()),
            **({"If-Match": f'"case-v{version}"'} if version else {}),
        }

    def read(cid):
        return client.get(prefix + "/cases/" + cid).json()

    initial = client.post(
        prefix + "/intakes/files",
        headers=headers(),
        json={"rights_attestation": "authored-synthetic", "files": packet_files()},
    )
    assert initial.status_code == 202, initial.text
    cid = initial.json()["case_id"]
    url = prefix + "/cases/" + cid
    c = read(cid)
    request = client.post(
        url + "/information-requests",
        headers=headers(c["version"]),
        json={
            "fields": ["received_quantity"],
            "reason": "Please attach both receiving records",
        },
    )
    assert request.status_code == 201, request.text
    c = read(cid)
    assert c["state"] == "awaiting_info"
    r1, r2 = "GR-" + uuid4().hex, "GR-" + uuid4().hex
    files = [
        receipt_file(r1, "40.1"),
        receipt_file(r2, "59.9", xlsx=True, currency=None),
    ]
    attached = client.post(
        prefix + "/intakes/files",
        headers=headers(c["version"]),
        json={
            "rights_attestation": "authored-synthetic",
            "case_id": cid,
            "files": files,
        },
    )
    assert attached.status_code == 202, attached.text
    c = read(cid)
    assert c["received_quantity"] == "100" and not c["missing_fields"]
    assert c["information_requests"][0]["state"] == "resolved"
    version = c["version"]
    duplicate = client.post(
        prefix + "/intakes/files",
        headers=headers(version),
        json={
            "rights_attestation": "authored-synthetic",
            "case_id": cid,
            "files": files,
        },
    )
    assert duplicate.status_code == 202 and duplicate.json()["duplicate"] is True
    assert read(cid)["version"] == version
    rid = str(uuid4())
    seed(WORKSPACE, rid)
    matched = client.post(
        url + "/target-match", headers=headers(version), json={"selected_target": rid}
    )
    assert matched.status_code == 200, matched.text
    c = read(cid)
    for quantity in ["-1", "100.000001"]:
        invalid = client.post(
            url + "/decisions",
            headers=headers(c["version"]),
            json={
                "mode": "partial",
                "quantity": quantity,
                "reason": "Bounds test",
                "evidence_digest": c["source_digest"],
            },
        )
        assert invalid.status_code == 422, invalid.text
    proposal = client.post(
        url + "/decisions",
        headers=headers(c["version"]),
        json={
            "mode": "partial",
            "quantity": "100",
            "reason": "Only both independently received lines",
            "evidence_digest": c["source_digest"],
        },
    )
    assert proposal.status_code == 201, proposal.text
    p = proposal.json()["proposal"]
    duplicate = client.post(
        prefix + "/intakes/files",
        headers=headers(read(cid)["version"]),
        json={
            "rights_attestation": "authored-synthetic",
            "case_id": cid,
            "files": files,
        },
    )
    assert duplicate.status_code == 202 and read(cid)["proposal"]["state"] == "pending"
    assert [a["allocated"] for a in p["allocation_manifest"]["rows"]] == [
        "40.1",
        "59.9",
    ]
    assert p["allocation_manifest"]["rows"][0]["currency_status"] == "declared"
    assert p["allocation_manifest"]["rows"][1]["currency_status"] == "derived_from_case"
    assert p["allocation_manifest"]["rows"][1]["currency_basis"]["case_version"] == p["case_version"]
    assert (
        p["retained_balance"] == "1036.80" and p["patch"]["gross_amount"] == "5184.00"
    )
    with connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM jobs WHERE proposal=%s", (p["id"],)
            ).fetchone()[0]
            == 0
        )
    # Unsealed correction preserves old exact payload but invalidates its approval.
    c = read(cid)
    stale = client.patch(
        url + "/fields/invoice",
        headers=headers(c["version"] - 1),
        json={"typed_value": "INV-1048", "reason": "Stale review"},
    )
    assert stale.status_code == 412
    correction = client.patch(
        url + "/fields/invoice",
        headers=headers(c["version"]),
        json={
            "typed_value": "INV-1048",
            "reason": "Confirm supported invoice identity",
        },
    )
    assert correction.status_code == 200
    approver = TestClient(app)
    ah = session(approver, "approver")
    denied = approver.post(
        prefix + f"/proposals/{p['id']}/approvals",
        headers={**ah, "If-Match": '"proposal-v1"'},
        json={"intent_digest": p["digest"], "confirmation": "approve"},
    )
    assert denied.status_code == 412, denied.text
    c = read(cid)
    result = client.post(
        url + "/decisions",
        headers=headers(c["version"]),
        json={
            "mode": "partial",
            "quantity": "100",
            "reason": "Re-reviewed both source lines",
            "evidence_digest": c["source_digest"],
        },
    )
    assert result.status_code == 201, result.text
    final = result.json()["proposal"]
    ah["Idempotency-Key"] = str(uuid4())
    approval = approver.post(
        prefix + f"/proposals/{final['id']}/approvals",
        headers={**ah, "If-Match": '"proposal-v1"'},
        json={"intent_digest": final["digest"], "confirmation": "approve"},
    )
    assert approval.status_code == 201, approval.text
    execute(claim(approval.json()["job_id"]))
    done = read(cid)
    assert done["verification"]["outcome"] == "matched", done
    assert (
        done["verification"]["observation"]["receipt"]["after_snapshot"]["gross_amount"]
        == "5184.00"
    )
    # Read the independent ledger: exactly one effect, and invoice face/identity unchanged.
    import psycopg

    from folio.target import DSN

    with psycopg.connect(DSN) as target_db:
        effects = target_db.execute(
            "SELECT body FROM operations WHERE operation_key=%s",
            (final["operation_key"],),
        ).fetchall()
    assert len(effects) == 1
    effect = effects[0][0]
    assert effect["after_revision"] == effect["before_revision"] + 1
    for field in [
        "invoice_face_amount",
        "supplier",
        "invoice_key",
        "bank_fingerprint",
        "due_date",
    ]:
        assert effect["before_snapshot"][field] == effect["after_snapshot"][field]
    assert (
        final["allocation_manifest"]["input_case_version"] + 1 == final["case_version"]
    )
    # Sealed source history cannot be rewritten.
    sealed = client.post(
        prefix + "/intakes/files",
        headers=headers(done["version"]),
        json={
            "rights_attestation": "authored-synthetic",
            "case_id": cid,
            "files": [receipt_file(r1, "40.1")],
        },
    )
    assert sealed.status_code == 409
    with connect() as db:
        old = db.execute(
            "SELECT body FROM proposals WHERE id=%s", (p["id"],)
        ).fetchone()[0]
    assert (
        old["digest"] == p["digest"]
        and old["allocation_manifest"] == p["allocation_manifest"]
    )
    # Same receipt under a different imported packet cannot be counted a second time.
    imported = client.post(
        prefix + "/intakes/files",
        headers=headers(),
        json={
            "rights_attestation": "authored-synthetic",
            "files": packet_files() + files[:1],
        },
    )
    other = read(imported.json()["case_id"])
    rid2 = str(uuid4())
    seed(WORKSPACE, rid2)
    assert (
        client.post(
            prefix + f"/cases/{other['id']}/target-match",
            headers=headers(other["version"]),
            json={"selected_target": rid2},
        ).status_code
        == 200
    )
    other = read(other["id"])
    reused = client.post(
        prefix + f"/cases/{other['id']}/decisions",
        headers=headers(other["version"]),
        json={
            "mode": "partial",
            "quantity": "1",
            "reason": "Must not reuse allocated receipt",
            "evidence_digest": other["source_digest"],
        },
    )
    assert reused.status_code == 422 and "UNALLOCATED" in reused.text, reused.text


def test_negative_receipts_fail_and_explicit_dimensions_are_preserved():
    result = extract_packet([receipt("bad.csv", "GR-N", "-1")])
    assert "RECEIPT_QUANTITY_INVALID" in result["receipt_manifest"]["errors"]
    assert "received_quantity" not in result["values"]
    for unit, currency in [("kg", "USD"), ("each", "EUR")]:
        manifest = receipt_manifest(
            [receipt("explicit.csv", "GR-N", "4", unit=unit, currency=currency)]
        )
        assert manifest["errors"] == []
        assert manifest["rows"][0]["unit"] == unit
        assert manifest["rows"][0]["currency"] == currency
    result = extract_packet(
        [receipt("one.csv", "GR-X", "4"), receipt("alias.xlsx", "GR-X", "5")]
    )
    assert "RECEIPT_IDENTITY_CONFLICT" in result["receipt_manifest"]["errors"]


def test_four_decimal_price_is_not_silently_truncated_by_extraction():
    # Round 6 (confirm-or-refuse): the price is reported with a consistent set.
    source = parsed(
        "price.csv",
        b"Quantity,10000\nUnit price USD,48.0001\nTax rate,0%\nInvoice total USD,480001.00\n",
        "invoice",
    )
    assert extract_packet([source])["values"]["unit_price"] == "48.0001"
