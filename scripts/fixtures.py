import hashlib
import json
from pathlib import Path

from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parents[1]


def generate():
    out = ROOT / "fixtures/public"
    out.mkdir(parents=True, exist_ok=True)
    docs = {
        "invoice": [
            "ASTER SUPPLY CO.",
            "Invoice INV-1048",
            "Purchase order PO-0732",
            "Quantity 120",
            "Unit price USD 48.00",
            "Tax rate 8%",
            "Invoice face total USD 6220.80",
        ],
        "purchase-order": [
            "NORTHSTAR STUDIO",
            "Purchase order PO-0732",
            "Supplier Aster Supply Co.",
            "Ordered quantity 120",
            "Unit price USD 48.00",
        ],
        "receipt": [
            "NORTHSTAR STUDIO",
            "Goods receipt GR-208",
            "Purchase order PO-0732",
            "Received quantity 100",
            "Unit each",
        ],
    }
    manifest = {
        "authorship": "Independently authored synthetic test assets by Folio implementation; not private documents",
        "license": "CC0-1.0",
        "files": [],
    }
    for name, lines in docs.items():
        path = out / (name + ".pdf")
        c = Canvas(str(path), invariant=1)
        c.setTitle("Folio synthetic " + name)
        for i, line in enumerate(["FOLIO AUTHOR-CREATED SYNTHETIC FIXTURE"] + lines):
            c.drawString(60, 760 - i * 36, line)
        c.save()
        manifest["files"].append(
            {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    (out / "rights-manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    generate()
