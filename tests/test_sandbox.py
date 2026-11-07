from pathlib import Path

import pytest


def test_actual_sandbox_csv_has_no_network_mount_or_write_authority():
    from folio.sandbox import parse_isolated

    result = parse_isolated(b"key,value\nquantity,100\n", "receipt.csv")
    assert result["cells"][-1]["value"] == "100"
    boundary = result["containment"]
    assert boundary["network"] == "none" and boundary["read_only_root"] is True
    assert boundary["memory_bytes"] == 536870912
    assert boundary["memory_swap_bytes"] == 536870912
    assert boundary["pids_limit"] == 64
    assert boundary["cpu_seconds_limit"] == 60
    assert boundary["wall_seconds_limit"] == 120
    assert boundary["tmpfs"]["/tmp"].endswith("size=256m")
    assert boundary["host_mounts"] == []
    assert boundary["user"] == "65534:65534"


def test_sandbox_rejects_unsupported_format_without_host_parse():
    from folio.sandbox import parse_isolated

    with pytest.raises(ValueError, match="FORMAT_UNSUPPORTED"):
        parse_isolated(b"#!/bin/sh\nexit 0", "payload.sh")


def test_sandbox_rejects_oversize_before_checking_for_docker(monkeypatch):
    from folio.intake_limits import MAX_FILE_BYTES
    from folio.sandbox import parse_isolated

    monkeypatch.setattr(
        "shutil.which", lambda _name: pytest.fail("oversize reached Docker discovery")
    )
    with pytest.raises(ValueError, match="FILE_REJECTED"):
        parse_isolated(b"A" * (MAX_FILE_BYTES + 1), "oversize.csv")


def test_scanned_pdf_is_real_ocr_with_word_boxes_and_confidence():
    import io

    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    from folio.sandbox import parse_isolated

    image = Image.new("RGB", (1200, 500), "white")
    draw = ImageDraw.Draw(image)
    font_path = next(
        (
            f
            for f in (
                "C:/Windows/Fonts/arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            )
            if Path(f).is_file()
        ),
        None,
    )
    if font_path is None:
        pytest.skip("no TrueType font available to render the OCR fixture")
    font = ImageFont.truetype(font_path, 48)
    draw.text((60, 80), "Receipt GR-208", font=font, fill="black")
    draw.text((60, 170), "Received quantity 100", font=font, fill="black")
    buf = io.BytesIO()
    canvas = Canvas(buf, pagesize=(600, 250))
    canvas.drawImage(ImageReader(image), 0, 0, width=600, height=250)
    canvas.save()
    parsed = parse_isolated(buf.getvalue(), "scan.pdf")
    page = parsed["pages"][0]
    assert "100" in page["text"] and "Received" in page["text"]
    assert page["text_kind"] == "ocr-word-order-v1"
    for word in page["words"]:
        assert page["text"][word["start"] : word["end"]] == word["quote"]
        assert (
            word["geometry_kind"] == "ocr-word-box" and 0 <= word["confidence"] <= 100
        )
        assert word["boxes"]
