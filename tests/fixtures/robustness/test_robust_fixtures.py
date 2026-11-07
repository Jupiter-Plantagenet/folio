"""Round-5 regression fixtures parsed by the real container parser.

set/cases/*.json hold the parser output produced by eval/prepare_cases.py
(networkless container, Tesseract OCR for scans). These tests re-run the
current extractor on that frozen parser output and compare it with the
author labels in labels.json; a docker-marked test re-parses a sample of the
original files to show the frozen output is genuine.
"""

import importlib.util
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from folio.persisted_receipts import manifest
from folio.provenance import validate_citation

HERE = Path(__file__).resolve().parent
LABELS = json.loads((HERE / "labels.json").read_text(encoding="utf-8"))["tasks"]
CASES = {p.stem: json.loads(p.read_text()) for p in sorted((HERE / "set" / "cases").glob("*.json"))}
_spec = importlib.util.spec_from_file_location("robustness_extraction_grade", HERE / "check_extraction.py")
_grader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_grader)
grade = _grader.grade


def test_fixture_set_is_complete():
    assert set(CASES) == set(LABELS) and len(CASES) == 20
    assert all(not c["rejected"] for c in CASES.values())


@pytest.mark.parametrize("task", sorted(LABELS))
def test_extraction_matches_author_labels(task):
    problems, _, _ = grade(CASES[task], LABELS[task]["expect"])
    assert not problems, problems


@pytest.mark.parametrize("task", sorted(LABELS))
def test_every_reading_cites_exact_source_text(task):
    from folio.provenance import extract_packet

    sources = CASES[task]["sources"]
    ex = extract_packet(sources)
    for entries in ex["field_provenance"].values():
        for entry in entries:
            src = entry.get("source") or {}
            if "start" in src and "cell" not in src:
                validate_citation(sources, src)
    m = ex["receipt_manifest"]
    if m["rows"] and not m["errors"]:
        manifest(m)
    if ex["invoice_supplier"]:
        validate_citation(sources, ex["invoice_supplier"]["source"])


def test_no_fixture_reads_a_wrong_value():
    """Unresolved is allowed only where labelled; a wrong value never is."""
    for task, case in CASES.items():
        problems, _, _ = grade(case, LABELS[task]["expect"])
        assert not [p for p in problems if "got" in p], (task, problems)


docker = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "image", "inspect", "folio-parser:1"], capture_output=True).returncode,
    reason="Docker parser image unavailable",
)


@docker
@pytest.mark.parametrize(
    "task,name",
    [("R02-two-column-de", "invoice.pdf"), ("R14-ocr-receipt", "receipt.pdf"), ("R18-xlsx-receipt-note", "receipt.xlsx")],
)
def test_frozen_parser_output_is_reproducible(task, name):
    from folio.sandbox import parse_isolated

    raw = (HERE / "set" / "packets" / task / name).read_bytes()
    for _ in range(40):
        try:
            parsed = parse_isolated(raw, name)
            break
        except ValueError as error:
            if str(error) != "PARSER_CAPACITY_RETRY":
                raise
            time.sleep(0.25)
    stored = next(s for s in CASES[task]["sources"] if s["name"] == name)
    assert parsed["sha256"] == stored["sha256"]
    assert [p["text"] for p in parsed["pages"]] == [p["text"] for p in stored["pages"]]
