import hashlib


def test_real_pdf_words_have_exact_quote_geometry_and_digest():
    import io

    from reportlab.pdfgen.canvas import Canvas

    from folio.parser import parse_pdf

    buffer = io.BytesIO()
    c = Canvas(buffer)
    c.drawString(60, 750, "Invoice INV-1048")
    c.drawString(60, 725, "Quantity 120")
    c.save()
    raw = buffer.getvalue()
    result = parse_pdf(raw)
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    page = result["pages"][0]
    assert "INV-1048" in page["text"]
    for word in page["words"]:
        assert page["text"][word["start"] : word["end"]] == word["quote"]
        assert len(word["boxes"]) > 0
        for box in word["boxes"]:
            assert 0 <= box[0] < box[2] <= page["width"]
