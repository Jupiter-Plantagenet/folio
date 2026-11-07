"""Encrypted PDF/XLSX inputs are refused per file, never decrypted or guessed."""

import base64
import io

import pytest
from fastapi.testclient import TestClient


def _encrypted_pdf(user_password):
    from reportlab.lib import pdfencrypt
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    encrypt = pdfencrypt.StandardEncryption(user_password, "folio-owner-fixture")
    pdf = canvas.Canvas(buffer, encrypt=encrypt)
    pdf.drawString(72, 720, "Invoice INV-ENC-1 quantity 100")
    pdf.save()
    return buffer.getvalue()


def _plain_pdf():
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "Invoice INV-PLAIN-1 quantity 100")
    pdf.save()
    return buffer.getvalue()


# OLE2 compound-file header, as written by Excel for a password-protected workbook.
ENCRYPTED_XLSX = bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 504


@pytest.mark.parametrize("user_password", ["", "open-sesame"])
def test_encrypted_pdf_is_rejected_even_with_empty_user_password(user_password):
    from folio.formats import parse_document

    with pytest.raises(ValueError, match="ENCRYPTED_REJECTED"):
        parse_document(_encrypted_pdf(user_password), "invoice.pdf")


def test_encrypted_xlsx_is_rejected_before_zip_parsing():
    from folio.formats import parse_document

    with pytest.raises(ValueError, match="ENCRYPTED_REJECTED"):
        parse_document(ENCRYPTED_XLSX, "receipt.xlsx")


def test_plain_pdf_still_parses():
    from folio.formats import parse_document

    parsed = parse_document(_plain_pdf(), "invoice.pdf")
    assert "INV-PLAIN-1" in parsed["pages"][0]["text"]


def test_encrypted_files_in_a_batch_are_rejected_without_losing_accepted_file():
    from test_domain_completion import session

    from folio.workbench import WORKSPACE, app, migrate

    migrate()
    client = TestClient(app)
    headers = session(client)

    def b64(raw):
        return base64.b64encode(raw).decode()

    result = client.post(
        f"/api/v1/workspaces/{WORKSPACE}/intakes/files",
        headers=headers,
        json={
            "rights_attestation": "authored-synthetic",
            "files": [
                {
                    "name": "receipt.csv",
                    "document_role": "receipt",
                    "base64": b64(
                        b"Goods receipt,GR-ENC\nReceived quantity,100\nUnit,each\n"
                    ),
                },
                {
                    "name": "invoice.pdf",
                    "document_role": "invoice",
                    "base64": b64(_encrypted_pdf("")),
                },
                {
                    "name": "order.xlsx",
                    "document_role": "purchase_order",
                    "base64": b64(ENCRYPTED_XLSX),
                },
            ],
        },
    )
    assert result.status_code == 202, result.text
    body = result.json()
    assert len(body["accepted"]) == 1
    assert len(body["rejected"]) == 2
    assert all("ENCRYPTED_REJECTED" in str(item) for item in body["rejected"])
    case = client.get(
        f"/api/v1/workspaces/{WORKSPACE}/cases/" + body["case_id"]
    ).json()
    assert case["received_quantity"] == "100"
