"""Round-5 delivery inventory: Python/npm/model/container licence inventory, a secret-pattern
scan of the delivered source, and a SHA256 source-identity manifest.

Source set = every file under folio/ that git does not ignore (.gitignore), excluding
evidence/ logs and docs/round*-logs (raw evidence, inventoried separately). The repository
has no commits, so "tracked" means "not ignored" here; this is stated in the output.
Output: evidence/documents/delivery/round5/{licenses.json,secret-scan.json,source-manifest.sha256}
"""

import hashlib
import json
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/documents/delivery/round5"
PERMISSIVE = re.compile(r"\b(MIT|BSD|Apache|ISC|PSF|Python Software Foundation|MPL|0BSD|Unlicense|CC0|Zlib|BlueOak|HPND|CC-BY|WTFPL|Public Domain)", re.I)
COPYLEFT = re.compile(r"\b(A?GPL|LGPL|SSPL|EUPL|CDDL|EPL)", re.I)


def classify(lic):
    if not lic:
        return "unknown"
    if COPYLEFT.search(lic) and not PERMISSIVE.search(lic):
        return "copyleft-review"
    if COPYLEFT.search(lic):
        return "dual-or-mixed-review"
    return "permissive" if PERMISSIVE.search(lic) else "other-review"


def python_inventory():
    locked = {}
    for line in (ROOT / "requirements.lock").read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)", line.strip())
        if m:
            locked[m.group(1).lower().replace("_", "-")] = m.group(2)
    rows = []
    for d in metadata.distributions():
        name = d.metadata["Name"]
        lic = d.metadata.get("License-Expression") or d.metadata.get("License") or ""
        if not lic or len(lic) > 80:
            classifiers = [c.split("::")[-1].strip() for c in d.metadata.get_all("Classifier") or [] if c.startswith("License ::")]
            lic = "; ".join(classifiers) or (lic[:80] + "..." if lic else "")
        key = name.lower().replace("_", "-")
        rows.append({"name": name, "version": d.version, "license": lic, "class": classify(lic),
                     "in_requirements_lock": key in locked, "lock_version_matches": locked.get(key) == d.version})
    rows.sort(key=lambda r: r["name"].lower())
    return rows, locked


def npm_inventory():
    lock = json.loads((ROOT / "web/package-lock.json").read_text())
    rows = []
    for path, meta in lock.get("packages", {}).items():
        if not path:
            continue
        pj = ROOT / "web" / path / "package.json"
        lic = meta.get("license")
        if not lic and pj.exists():
            try:
                lic = json.loads(pj.read_text(encoding="utf-8")).get("license")
            except ValueError:
                pass
        if isinstance(lic, dict):
            lic = lic.get("type")
        rows.append({"name": path.split("node_modules/")[-1], "version": meta.get("version"), "license": lic,
                     "class": classify(lic or ""), "dev": bool(meta.get("dev")), "installed": pj.exists()})
    return rows


def source_files():
    r = subprocess.run(["git", "ls-files", "--others", "--cached", "--exclude-standard"], cwd=ROOT,
                       capture_output=True, text=True, check=True)
    skip = (re.compile(r"^(evidence/|evaluation-sealed/|docs/round\d+-logs/|evidence-.*\.log$|web/live-tests/.*\.png$)"))
    return sorted(p for p in r.stdout.splitlines() if not skip.match(p))


SECRET_PATTERNS = {
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "openai_or_anthropic_key": re.compile(r"\bsk-(ant-)?[A-Za-z0-9_\-]{20,}"),
    "hf_token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "dsn_with_password": re.compile(r"postgres(?:ql)?://[^:/\s\"']+:[^@\s\"']{6,}@"),
    "slack_token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
}


def secret_scan(files):
    hits = []
    for rel in files:
        p = ROOT / rel
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, rx in SECRET_PATTERNS.items():
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                frag = m.group(0)
                hits.append({"file": rel, "line": line, "pattern": name,
                             "fingerprint": hashlib.sha256(frag.encode()).hexdigest()[:12]})
    return hits


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    py, locked = python_inventory()
    npm = npm_inventory()
    model_dir = ROOT / ".cache/models/qwen3-8b-gguf-7c41481f"
    model = {
        "name": "Qwen/Qwen3-8B-GGUF Qwen3-8B-Q4_K_M.gguf", "revision": "7c41481f57cb95916b40956ab2f0b139b296d974",
        "license_file_sha256": hashlib.sha256((model_dir / "LICENSE").read_bytes()).hexdigest(),
        "license_first_line": (model_dir / "LICENSE").read_text(errors="replace").strip().splitlines()[0][:80],
        "weights_sha256_pinned": (model_dir / "SHA256").read_text().split()[0] if (model_dir / "SHA256").exists() else None,
        "engine": "llama.cpp build 10964 (b29c606e2) CUDA win32-x64 binary (MIT; NVIDIA CUDA runtime DLLs under NVIDIA EULA)",
    }
    container = {"parser_image": "folio-parser:1 FROM python:3.12-slim@sha256:229a2c5b... + Debian tesseract-ocr (Apache-2.0) "
                 "+ pdfplumber 0.11.9 (MIT), openpyxl 3.1.5 (MIT), defusedxml 0.7.1 (PSF), pytesseract 0.3.13 (Apache-2.0); "
                 "Debian base packages carry their own (mostly GPL/LGPL system) licences - aggregate, not linked into Folio"}
    files = source_files()
    hits = secret_scan(files)
    manifest = []
    for rel in files:
        manifest.append(f"{hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()}  {rel}")
    # LF only (Windows text mode would write CRLF and break `sha256sum -c`/`cut` consumers).
    (OUT / "source-manifest.sha256").write_bytes(("\n".join(manifest) + "\n").encode())
    summary = {
        "python": {"count": len(py), "by_class": {}, "review": [r for r in py if r["class"] != "permissive"],
                   "not_in_lock": [r["name"] for r in py if not r["in_requirements_lock"]],
                   "lock_mismatch": [r["name"] for r in py if r["in_requirements_lock"] and not r["lock_version_matches"]]},
        "npm": {"count": len(npm), "by_class": {}, "review": [r for r in npm if r["class"] != "permissive"]},
        "model": model, "container": container,
        "source_manifest": {"files": len(files), "sha256_of_manifest": hashlib.sha256((OUT / "source-manifest.sha256").read_bytes()).hexdigest(),
                            "definition": "git ls-files --others --cached --exclude-standard minus evidence/, evaluation-sealed/ (evaluator-only labels) and raw round logs (no commits exist)"},
        "secret_scan": {"files_scanned": len(files), "hits": hits},
    }
    for r in py:
        summary["python"]["by_class"][r["class"]] = summary["python"]["by_class"].get(r["class"], 0) + 1
    for r in npm:
        summary["npm"]["by_class"][r["class"]] = summary["npm"]["by_class"].get(r["class"], 0) + 1
    (OUT / "licenses.json").write_text(json.dumps({"python": py, "npm": npm, "model": model, "container": container}, indent=1))
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: (v if k != "secret_scan" else {"hits": len(v["hits"])}) for k, v in summary.items()
                      if k in ("source_manifest", "secret_scan")}, indent=1))
    print(json.dumps({"python_by_class": summary["python"]["by_class"], "npm_by_class": summary["npm"]["by_class"]}))


if __name__ == "__main__":
    sys.exit(main())
