from uuid import uuid4

from test_receipt_allocations import packet_files, parsed, receipt_file
from test_receipt_lifecycle import hdr, setup


def test_same_receipt_cannot_gain_capacity_through_a_changed_import():
    from folio.target import seed
    from folio.workbench import WORKSPACE

    receipt_id = "GR-" + uuid4().hex
    for quantity, recognition, xlsx in [("10", "8", False), ("20", "9", True)]:
        client, auth, prefix, url = setup(
            packet_files() + [receipt_file(receipt_id, quantity, xlsx=xlsx)]
        )
        c = client.get(url).json()
        rid = str(uuid4())
        seed(WORKSPACE, rid)
        assert (
            client.post(
                url + "/target-match",
                headers=hdr(auth, c),
                json={"selected_target": rid},
            ).status_code
            == 200
        )
        c = client.get(url).json()
        response = client.post(
            url + "/decisions",
            headers=hdr(auth, c),
            json={
                "mode": "partial",
                "quantity": recognition,
                "reason": "Validate cross-import receipt identity",
                "evidence_digest": c["source_digest"],
            },
        )
        if not xlsx:
            assert response.status_code == 201, response.text
        else:
            assert (
                response.status_code == 422 and "IDENTITY_CONFLICT" in response.text
            ), response.text


def test_explicit_currency_disagreement_cannot_hide_behind_usd_price_label():
    from folio.provenance import extract_packet

    result = extract_packet(
        [parsed("invoice.csv", b"Unit price USD,48.00\nCurrency,EUR\n", "invoice")]
    )
    assert "currency" in result["conflicts"]
