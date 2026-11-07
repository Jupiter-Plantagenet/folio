"""DOC-T-PROVENANCE hostile set, parsed through the real networkless parser container.

Fixtures: fixtures/hostile (scripts/hostile_fixtures.py, CC0, ground truth in
fixtures/hostile/truth). Every parse goes through folio.sandbox.parse_isolated
(docker image folio-parser:1, --network none, read-only, uid 65534). Per-case
results are written to evidence/documents/provenance/round6-<UTC>/results.json.
The intake/correction test needs a disposable database and only runs under
scripts/isolated_tests.py.
"""

import hashlib
import json
import os
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures/hostile"
MANIFEST = json.loads((FIX / "manifest.json").read_text(encoding="utf-8"))
ENTRIES = {e["name"]: e for e in MANIFEST["files"]}
STAMP = os.environ.get("FOLIO_ROUND6_STAMP") or datetime.now(timezone.utc).strftime(
    "%Y%m%dT%H%M%SZ"
)
EVIDENCE = ROOT / "evidence/documents/provenance" / f"round6-{STAMP}"
RESULTS = {"stamp": STAMP, "suite": "DOC-T-PROVENANCE", "cases": {}, "image_ids": set()}
_CACHE = {}
ISOLATED = os.environ.get("FOLIO_PROOF_PROXY_PORT") == "18291"


def truth(name):
    return json.loads((FIX / ENTRIES[name]["truth"]).read_text(encoding="utf-8"))


def parse(name, raw=None):
    """Parse via the container once per name; returns (result, error)."""
    if name not in _CACHE:
        from folio.sandbox import parse_isolated

        data = raw if raw is not None else (FIX / name).read_bytes()
        try:
            out = parse_isolated(data, name)
            RESULTS["image_ids"].add(out["containment"]["image_id"])
            _CACHE[name] = (out, None)
        except ValueError as error:
            _CACHE[name] = (None, str(error))
    return _CACHE[name]


def record(name, **fields):
    RESULTS["cases"].setdefault(name, {}).update(fields)


@pytest.fixture(scope="module", autouse=True)
def write_results():
    yield
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = {**RESULTS, "image_ids": sorted(RESULTS["image_ids"])}
    out["fixture_manifest_sha256"] = hashlib.sha256(
        (FIX / "manifest.json").read_bytes()
    ).hexdigest()
    (EVIDENCE / "results.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def same_degenerate(a, b, tol=0.05):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ------------------------------------------------------------- fixtures

def test_fixture_bytes_and_generator_are_reproducible():
    for entry in MANIFEST["files"]:
        assert hashlib.sha256((FIX / entry["name"]).read_bytes()).hexdigest() == entry["sha256"]
        if "truth" in entry:
            assert (
                hashlib.sha256((FIX / entry["truth"]).read_bytes()).hexdigest()
                == entry["truth_sha256"]
            )
    check = subprocess.run(
        [sys.executable, "scripts/hostile_fixtures.py", "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    record("generator-check", stdout=check.stdout.strip(), exit_code=check.returncode)
    assert check.returncode == 0, check.stdout + check.stderr
    assert MANIFEST["license"] == "CC0-1.0"


GEOM = sorted(n for n in ENTRIES if n.startswith("geom-"))
UNI = sorted(n for n, e in ENTRIES.items() if n.startswith("uni-") and "truth" in e)
OCR = sorted(n for n in ENTRIES if n.startswith("ocr-"))


@pytest.mark.parametrize("name", GEOM)
def test_ground_truth_boxes_contain_the_rendered_ink(name):
    """Independent check of the oracle: pdfium (host, own fixture) draws ink only inside truth boxes."""
    import pypdfium2

    t = truth(name)
    scale = 4
    doc = pypdfium2.PdfDocument((FIX / name).read_bytes())
    try:
        image = doc[0].render(scale=scale).to_pil().convert("L")
    finally:
        doc.close()
    assert abs(image.width - t["width"] * scale) <= 1 and abs(image.height - t["height"] * scale) <= 1
    boxes = [b for w in t["words"] for b in w["boxes"]]
    ink = outside = 0
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            if pixels[x, y] < 128:
                ink += 1
                px, py = (x + 0.5) / scale, (y + 0.5) / scale
                if not any(b[0] - 0.75 <= px <= b[2] + 0.75 and b[1] - 0.75 <= py <= b[3] + 0.75 for b in boxes):
                    outside += 1
    record(name, truth_ink_pixels=ink, truth_ink_outside_boxes=outside)
    assert ink > 500 and outside / ink < 0.005


# ------------------------------------------------------------- geometry

def _char_boxes(page):
    return [b for w in page["words"] for b in w["boxes"]]


@pytest.mark.parametrize("name", GEOM + UNI)
def test_digital_geometry_iou_and_zero_invented_boxes(name):
    t = truth(name)
    out, error = parse(name)
    assert error is None, error
    page = out["pages"][0]
    got = [w["quote"] for w in page["words"]]
    want = [w["text"] for w in t["words"]]
    record(name, words=len(got), words_match_truth=got == want)
    assert got == want  # reading order and exact text
    assert (page["width"], page["height"]) == pytest.approx((t["width"], t["height"]))
    ious, invented = [], 0
    for word, expect in zip(page["words"], t["words"]):
        assert len(word["boxes"]) == len(expect["boxes"])
        for box, ref in zip(word["boxes"], expect["boxes"]):
            if ref[2] - ref[0] < 1e-6:  # zero-advance mark (ZW/bidi/combining)
                if not same_degenerate(box, ref):
                    invented += 1
                continue
            value = iou(box, ref)
            ious.append(value)
            if value < 0.95:
                invented += 1
    # Every returned box is one real glyph's box; nothing outside the CropBox.
    assert len(_char_boxes(page)) == sum(len(w["boxes"]) for w in t["words"])
    hidden = [h for h in t["hidden"] if "char" in h]  # glyphs incl. spaces
    record(
        name,
        min_char_iou=min(ious),
        mean_char_iou=sum(ious) / len(ious),
        chars=len(ious),
        boxes_without_truth=invented,
        hidden_truth_chars=len(hidden),
        hidden_reported=page.get("hidden_chars_outside_cropbox"),
        hidden_text_extracted="999" in page["text"],
        rotation=page.get("rotation"),
        image_id=out["containment"]["image_id"],
    )
    assert min(ious) >= 0.95
    assert invented == 0
    assert "999" not in page["text"] and "hidden" not in page["text"]
    if hidden:
        assert page["hidden_chars_outside_cropbox"] == len(hidden)


@pytest.mark.parametrize("name", OCR)
def test_ocr_geometry_in_displayed_crop_frame_and_no_invented_boxes(name):
    t = truth(name)
    out, error = parse(name)
    assert error is None, error
    page = out["pages"][0]
    assert (page["width"], page["height"]) == pytest.approx((t["width"], t["height"]))
    # OCR renders at up to 300 dpi (lower only for very large pages); the raster
    # must match the page size at the recorded dpi.
    dpi = page["raster"]["dpi"]
    assert 72 <= dpi <= 300 and len(page["raster"]["sha256"]) == 64
    assert abs(page["raster"]["width"] - page["width"] * dpi / 72) <= 2
    assert abs(page["raster"]["height"] - page["height"] * dpi / 72) <= 2
    refs = t["ocr_words"]
    import pypdfium2

    doc = pypdfium2.PdfDocument((FIX / name).read_bytes())  # host render of own fixture
    try:
        ink = doc[0].render(scale=2).to_pil().convert("L")
    finally:
        doc.close()
    pixels = ink.load()
    invented, off_truth, matched = 0, 0, []
    for word in page["words"]:
        assert word["requires_review"] is True and word["geometry_kind"] == "ocr-word-box"
        box = word["boxes"][0]
        # Each OCR box must cover real printed ink in the displayed page.
        dark = sum(
            1
            for x in range(max(0, int(box[0] * 2)), min(ink.width, int(box[2] * 2) + 1))
            for y in range(max(0, int(box[1] * 2)), min(ink.height, int(box[3] * 2) + 1))
            if pixels[x, y] < 128
        )
        if dark == 0:  # no ink at all under the box
            invented += 1
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if not any(r["box"][0] - 2 <= cx <= r["box"][2] + 2 and r["box"][1] - 2 <= cy <= r["box"][3] + 2 for r in refs):
            off_truth += 1
        same = [r for r in refs if r["text"] == word["quote"]]
        if same:
            matched.append(max(iou(box, r["box"]) for r in same))
    sideways = "sideways" in name
    record(
        name,
        ocr_words=len(page["words"]),
        ocr_text=page["text"],
        ocr_words_matching_truth_text=len(matched),
        truth_words=len(refs),
        min_word_iou=min(matched) if matched else None,
        mean_word_iou=sum(matched) / len(matched) if matched else None,
        boxes_off_ink=invented,
        box_centres_outside_truth_word_boxes=off_truth,
        hidden_text_extracted="999" in page["text"],
    )
    assert invented == 0
    if not sideways:
        assert off_truth == 0
    assert "999" not in page["text"]
    if not sideways:
        assert len(matched) >= len(refs) - 1
        assert min(matched) >= 0.80
    else:
        from folio.provenance import extract_packet

        packet = extract_packet([{**out, "id": "sideways", "document_role": "receipt"}])
        assert packet["values"].get("received_quantity") != "100"


# ------------------------------------------------------------- spans / Unicode

def test_every_span_round_trips_from_hashed_immutable_text():
    spans = mismatches = 0
    for name in sorted(ENTRIES):
        if ENTRIES[name]["suite"] != "provenance":
            continue
        out, error = parse(name)
        if error:
            continue
        for page in out["pages"]:
            text = page["text"]
            assert hashlib.sha256(text.encode()).hexdigest() == page["text_sha256"]
            for word in page["words"]:
                spans += 1
                if text[word["start"] : word["end"]] != word["quote"]:
                    mismatches += 1
    record("all-provenance-fixtures", spans=spans, span_mismatches=mismatches)
    assert spans > 300 and mismatches == 0


def test_offsets_are_unicode_scalar_offsets_and_text_is_not_normalized():
    out, error = parse("uni-combining-supplementary.pdf")
    assert error is None, error
    page = out["pages"][0]
    expected = " ".join(w["text"] for w in truth("uni-combining-supplementary.pdf")["words"])
    assert page["text"] == expected  # exact scalars, nothing folded
    assert unicodedata.normalize("NFC", page["text"]) != page["text"]
    assert unicodedata.normalize("NFKC", page["text"]) != page["text"]
    assert "ﬁ" in page["text"] and "\U0001d400" in page["text"]
    ref = next(w for w in page["words"] if w["quote"] == "\U0001f4b5")
    utf16 = len(page["text"][: ref["start"]].encode("utf-16-le")) // 2
    assert ref["start"] == len(page["text"][: ref["start"]]) and utf16 > ref["start"]
    record(
        "uni-combining-supplementary.pdf",
        scalar_offset_of_banknote=ref["start"],
        utf16_offset_of_banknote=utf16,
        exact_text=True,
    )


INVOICE_CASES = {
    "uni-inv-control.pdf": {"invoice": "INV-1048", "invoice_quantity": "120", "invoice_face_amount": "6220.80", "tax_rate": "0.08"},
    "uni-inv-homoglyph-id.pdf": {"invoice": None},
    "uni-inv-zw-id-suffix.pdf": {"invoice": None},
    "uni-inv-zw-id-inner.pdf": {"invoice": None},
    "uni-inv-bidi-id.pdf": {"invoice": None},
    "uni-inv-zw-amounts.pdf": {"invoice_quantity": None, "invoice_face_amount": None},
    "uni-inv-isolate-qty.pdf": {"invoice_quantity": None},
    "uni-inv-arabic-digits.pdf": {"invoice_quantity": None, "tax_rate": None},
    "uni-inv-mathbold-issuer.pdf": {"supplier_check": "not-match"},
    "uni-inv-cyrillic-issuer.pdf": {"supplier_check": "not-match"},
    "uni-inv-ligature-issuer.pdf": {"supplier_check": "not-match"},
}


def _public(name, role):
    out, error = parse(name, (ROOT / "fixtures/public" / name).read_bytes())
    assert error is None, error
    return {**out, "id": role, "document_role": role}


@pytest.mark.parametrize("name", sorted(INVOICE_CASES))
def test_hostile_unicode_never_normalizes_into_a_clean_match(name):
    from folio.provenance import extract_packet

    out, error = parse(name)
    assert error is None, error
    sources = [
        {**out, "id": "invoice", "document_role": "invoice"},
        _public("purchase-order.pdf", "purchase_order"),
        _public("receipt.pdf", "receipt"),
    ]
    packet = extract_packet(sources)
    values = packet["values"]
    by_id = {s["id"]: s for s in sources}
    for field, entries in packet["field_provenance"].items():
        for entry in entries:
            src = entry.get("source") or {}
            if "start" in src and src.get("source_id") in by_id:
                page = by_id[src["source_id"]]["pages"][src["page"] - 1]
                assert page["text"][src["start"] : src["end"]] == src["quote"]
    for value in values.values():
        assert not any(unicodedata.category(ch) in ("Cf", "Cc") for ch in str(value))
    record(
        name,
        values={k: values.get(k) for k in ("invoice", "invoice_quantity", "invoice_face_amount", "tax_rate", "supplier")},
        conflicts=sorted(set(packet["conflicts"])),
        missing=packet["missing"],
        supplier_check=packet["supplier_check"],
    )
    for field, want in INVOICE_CASES[name].items():
        if field == "supplier_check":
            assert packet["supplier_check"]["status"] != "match"
        elif want is None:
            assert values.get(field) is None, (field, values.get(field))
            assert field in packet["missing"] or field in packet["conflicts"]
        else:
            assert values.get(field) == want
    if name == "uni-inv-control.pdf":
        assert packet["supplier_check"]["status"] == "match"


def test_nul_and_lone_surrogate_never_reach_storage():
    out, error = parse("uni-nul.pdf")
    record("uni-nul.pdf", accepted=error is None, error=error)
    assert out is None and "PARSER_OUTPUT_INVALID:NUL" in error
    out, error = parse("uni-lone-surrogate.pdf")
    record("uni-lone-surrogate.pdf", accepted=error is None, error=error,
           text=None if out is None else out["pages"][0]["text"])
    if out is not None:  # pdfminer replaced the unpaired surrogate; nothing unencodable
        out["pages"][0]["text"].encode("utf-8")
        assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in out["pages"][0]["text"])


# ------------------------------------------------------------- CSV / XLSX

CSV_OK = ["csv-utf8.csv", "csv-utf8-bom.csv", "csv-utf16le-bom.csv", "csv-utf16be-bom.csv",
          "csv-utf8-hostile-unicode.csv", "csv-inch-mark.csv"]


@pytest.mark.parametrize("name", CSV_OK)
def test_csv_encodings_decode_with_exact_byte_ranges(name):
    t = truth(name)
    raw = (FIX / name).read_bytes()
    out, error = parse(name)
    record(name, accepted=error is None, error=error)
    assert error is None, error
    assert out["csv"]["encoding"] == t["encoding"] and out["csv"]["bom"] == t["bom"]
    got = [(c["row"], c["column"], c["value"], c["quote"], c["byte_start"], c["byte_end"]) for c in out["cells"]]
    want = [(c["row"], c["column"], c["value"], c["quote"], c["byte_start"], c["byte_end"]) for c in t["cells"]]
    assert got == want
    for cell in out["cells"]:
        assert raw[cell["byte_start"] : cell["byte_end"]].decode(t["encoding"]) == cell["quote"]
    for word in out["pages"][0]["words"]:
        assert word["boxes"] == []  # no geometry is implied for cells
    record(name, cells=len(got), encoding=out["csv"]["encoding"], byte_ranges_match_truth=True)


def test_legacy_code_page_csv_fails_closed():
    out, error = parse("csv-cp1252.csv")
    record("csv-cp1252.csv", accepted=error is None, error=error,
           note="no BOM and not UTF-8; no declared-encoding input exists, so refused rather than guessed")
    assert out is None and "CSV_ENCODING_UNSUPPORTED" in error


def test_hostile_csv_receipt_values_are_not_accepted_quantities():
    from folio.provenance import extract_packet

    out, error = parse("csv-utf8-hostile-unicode.csv")
    assert error is None, error
    inv, err = parse("uni-inv-control.pdf")
    assert err is None
    packet = extract_packet([
        {**inv, "id": "invoice", "document_role": "invoice"},
        _public("purchase-order.pdf", "purchase_order"),
        {**out, "id": "receipt", "document_role": "receipt"},
    ])
    record("csv-utf8-hostile-unicode.csv", received_quantity=packet["values"].get("received_quantity"),
           manifest_errors=packet["receipt_manifest"]["errors"])
    assert packet["values"].get("received_quantity") in (None,)


@pytest.mark.parametrize("name", ["xlsx-dup-sheet-exact.xlsx", "xlsx-dup-sheet-case.xlsx", "xlsx-dup-sheet-nfd.xlsx"])
def test_duplicate_xlsx_sheets_are_rejected_never_merged(name):
    out, error = parse(name)
    record(name, accepted=error is None, error=error,
           sheets=None if out is None else sorted({(c["sheet"], c.get("sheet_id")) for c in out["cells"]}))
    if out is None:
        assert "XLSX_SHEET_NAME_DUPLICATE" in error
    else:  # acceptable only when every cell is disambiguated by sheet ID
        assert len({c.get("sheet_id") for c in out["cells"]}) == 2


def test_xlsx_formulas_are_literal_with_separate_cache_and_never_evaluated():
    from folio.provenance import extract_packet

    out, error = parse("xlsx-formulas.xlsx")
    assert error is None, error
    cells = {c["coordinate"]: c for c in out["cells"]}
    assert cells["B4"]["formula"] == "=60+40" and cells["B4"]["cached_value"] == 100
    assert cells["B5"]["formula"] == "=SUM(B2:B2)" and cells["B5"]["cached_value"] is None
    assert cells["B6"]["raw_formula"] == 'CONCATENATE("1","00")'
    assert all(cells[k]["requires_review"] for k in ("B4", "B5", "B6"))
    assert cells["B2"]["numeric_lexical"] == "100" and not cells["B2"]["requires_review"]
    packet = extract_packet([{**out, "id": "x", "document_role": "invoice"}])
    record("xlsx-formulas.xlsx", formula_cells={k: (cells[k]["formula"], cells[k]["cached_value"]) for k in ("B4", "B5", "B6")},
           sheet_id=cells["B4"].get("sheet_id"), values=packet["values"])


# ------------------------------------------------------------- intake + correction (DB)

@pytest.mark.skipif(not ISOLATED, reason="needs scripts/isolated_tests.py disposable database")
def test_intake_rejects_hostile_storage_and_human_correction_stays_distinct():
    import base64
    from uuid import uuid4

    from fastapi.testclient import TestClient
    from test_domain_completion import session

    from folio.workbench import WORKSPACE, app, migrate

    migrate()
    client = TestClient(app)
    h = session(client)
    url = f"/api/v1/workspaces/{WORKSPACE}/intakes/files"

    def item(path, role, name=None):
        return {"name": name or path.name, "document_role": role,
                "base64": base64.b64encode(path.read_bytes()).decode()}

    files = [
        item(FIX / "uni-inv-homoglyph-id.pdf", "invoice"),
        item(ROOT / "fixtures/public/purchase-order.pdf", "purchase_order"),
        item(ROOT / "fixtures/public/receipt.pdf", "receipt"),
        item(FIX / "uni-nul.pdf", "invoice"),
        item(FIX / "csv-utf8.csv", "receipt", name="../../escape.csv"),
        item(FIX / "csv-utf8.csv", "receipt", name="receipt.csv\u0000.exe"),
        item(FIX / "xlsx-dup-sheet-exact.xlsx", "receipt"),
    ]
    r = client.post(url, headers={**h, "Idempotency-Key": str(uuid4())},
                    json={"rights_attestation": "authored-synthetic", "files": files})
    assert r.status_code == 202, r.text
    body = r.json()
    rejected = {x["name"]: x["reason"] for x in body.get("rejected", [])}
    case = client.get(f"/api/v1/workspaces/{WORKSPACE}/cases/{body['case_id']}").json()
    record("intake-api", status=r.status_code, rejected=rejected,
           accepted=[s["name"] for s in case["sources"]], invoice=case.get("invoice"),
           conflicts=case.get("conflicting_fields"))
    assert "PARSER_OUTPUT_INVALID:NUL" in rejected["uni-nul.pdf"]
    assert rejected["../../escape.csv"] == "FILE_REJECTED"
    assert rejected["receipt.csv\\x00.exe"] == "FILE_METADATA_INVALID"
    assert "XLSX_SHEET_NAME_DUPLICATE" in rejected["xlsx-dup-sheet-exact.xlsx"]
    assert case.get("invoice") != "INV-1048"
    invoice_src = next(s for s in case["sources"] if s["name"] == "uni-inv-homoglyph-id.pdf")
    page = invoice_src["pages"][0]
    start = page["text"].index("ІNV-1048")
    unsafe = client.patch(
        f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}/fields/invoice",
        headers={**h, "Idempotency-Key": str(uuid4()), "If-Match": f'"case-v{case["version"]}"'},
        json={"typed_value": "INV-1048​", "reason": "zero-width must be refused"},
    )
    assert unsafe.status_code == 422 and "UNSAFE" in unsafe.text
    missing_identity = client.patch(
        f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}/fields/invoice",
        headers={**h, "Idempotency-Key": str(uuid4()), "If-Match": f'"case-v{case["version"]}"'},
        json={
            "typed_value": "INV-1048",
            "reason": "Citation missing immutable text identity must be rejected",
            "source": {
                "source_id": invoice_src["id"],
                "source_sha256": invoice_src["sha256"],
                "page": 1,
                "start": start,
                "end": start + 8,
                "quote": "ІNV-1048",
            },
        },
    )
    assert missing_identity.status_code == 422 and "CITATION_INVALID" in missing_identity.text
    fix = client.patch(
        f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}/fields/invoice",
        headers={**h, "Idempotency-Key": str(uuid4()), "If-Match": f'"case-v{case["version"]}"'},
        json={
            "typed_value": "INV-1048",
            "reason": "Reviewer asserts Latin identifier; printed id uses Cyrillic I",
            "source": {"source_id": invoice_src["id"], "source_sha256": invoice_src["sha256"],
                       "source_version": invoice_src.get("version", 1), "page": 1,
                       "text_artifact_sha256": hashlib.sha256(page["text"].encode("utf-8")).hexdigest(),
                       "start": start, "end": start + 8, "quote": "ІNV-1048"},
        },
    )
    assert fix.status_code == 200, fix.text
    after = client.get(f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}").json()
    version = after["field_provenance"]["invoice"][-1]
    record("human-correction", kind=version["kind"], actor=version["actor"],
           cited_quote=version["source"]["quote"], value=version["value"])
    assert version["kind"] == "human" and version["actor"] == "reviewer"
    assert version["source"]["quote"] == "ІNV-1048"  # evidence keeps the homoglyph
    assert after["invoice"] == "INV-1048"
    assert all(e["kind"] != "extracted" or e["value"] != "INV-1048"
               for e in after["field_provenance"]["invoice"])
