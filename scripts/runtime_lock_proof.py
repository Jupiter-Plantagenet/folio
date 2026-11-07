"""Unique immutable short-scratch mirror; disposable databases and proof ports only."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    label = sys.argv[1]
    selection = sys.argv[2:] or ["tests/test_export_runtime_lock.py"]
    stamp = str(time.time_ns())
    evidence = (
        ROOT
        / "evidence/documents/exports"
        / ("runtime-lock-fix-" + label + "-" + stamp)
    )
    evidence.mkdir(parents=True)
    mirror = Path("C:/Users/user/AppData/Local/hermes/cache/scratch") / (
        "fl" + stamp[-10:]
    )
    mirror.mkdir()
    pins = {}
    for folder in (
        "folio",
        "tests",
        "scripts",
        "ops",
        "contracts",
        "fixtures",
        "target-ledger",
        "docs",
        "web",
    ):
        for src in (ROOT / folder).rglob("*"):
            if src.is_file() and not any(
                p in src.parts
                for p in (
                    "__pycache__",
                    "node_modules",
                    "dist",
                    "test-results",
                    "playwright-report",
                )
            ):
                rel = src.relative_to(ROOT)
                dest = mirror / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
                pins[rel.as_posix()] = hashlib.sha256(dest.read_bytes()).hexdigest()
    for name in ("pytest.ini", "ruff.toml", "requirements.lock"):
        shutil.copyfile(ROOT / name, mirror / name)
        pins[name] = hashlib.sha256((mirror / name).read_bytes()).hexdigest()
    for folder in (
        "receipt-concurrency/fix-cycle-2",
        "proof",
        "flow",
        "finish-browser",
        "exports",
    ):
        (mirror / "evidence/documents" / folder).mkdir(parents=True, exist_ok=True)
    (evidence / "source-pins.json").write_text(json.dumps(pins, indent=2))
    sys.path[:0] = [str(mirror), str(mirror / "scripts")]
    if label == "browser":
        browser_env = {
            **os.environ,
            "PYTHONPATH": str(mirror),
            "TMPDIR": str(mirror / ".tmp"),
            "FOLIO_REAL_MODEL_TEST": "0",
        }
        for src, dest in (
            (ROOT / "web/node_modules", mirror / "web/node_modules"),
            (ROOT / ".cache/browsers", mirror / ".cache/browsers"),
        ):
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(dest), str(src)],
                check=True,
                capture_output=True,
            )
        result = subprocess.run(
            [sys.executable, "scripts/isolated_browser.py", "--trace=off"],
            cwd=mirror,
            env=browser_env,
            capture_output=True,
            text=True,
        )
        (evidence / "browser.log").write_text(
            result.stdout + result.stderr, encoding="utf-8"
        )
        shutil.copytree(mirror / "evidence", evidence / "legacy-output")
        (evidence / "manifest.json").write_text(
            json.dumps(
                {
                    "exit_code": result.returncode,
                    "mirror": str(mirror),
                    "source_count": len(pins),
                },
                indent=2,
            )
        )
        print("EVIDENCE", evidence, "EXIT", result.returncode)
        return result.returncode
    from scripts.isolated_database import create_profile
    from scripts.runtime import is_free, ready, stop

    assert is_free(18201) and is_free(18291), "Proof ports occupied; no process stopped"
    names, db_env = create_profile(mirror, "export" + stamp)
    from folio.identity import provision

    env = {
        **os.environ,
        **db_env,
        **provision(mirror / ".local/identities"),
        "PYTHONPATH": str(mirror) + os.pathsep + str(mirror / "scripts"),
        "FOLIO_SCHEMA_MODE": "bootstrap",
        "FOLIO_TARGET_URL": "http://127.0.0.1:18201",
        "FOLIO_PROOF_PROXY_PORT": "18291",
        "FOLIO_EXPORT_PROOF": "1",
        "FOLIO_EXPORT_ROOT": str(mirror / ".local/artifacts"),
        "FOLIO_EXPORT_EVIDENCE": str(evidence),
        "FOLIO_REAL_MODEL_TEST": "0",
        "TMPDIR": str(mirror / ".tmp"),
    }
    if label == "compat":
        env["FOLIO_EXPORT_MIGRATION_PROOF"] = "1"
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *selection,
        "-v",
        "-rs",
        "--junitxml=" + str(evidence / "suite.xml"),
    ]
    with (evidence / "target.log").open("w") as log:
        server = (
            None
            if label == "compat"
            else subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "folio.target:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "18201",
                ],
                cwd=mirror,
                env=env,
                stdout=log,
                stderr=log,
            )
        )
        try:
            if server:
                ready(18201)
            with (evidence / "pytest.log").open("w", encoding="utf-8") as output:
                result = subprocess.run(
                    cmd + ["-o", "faulthandler_timeout=60", "--durations=20"],
                    cwd=mirror,
                    env=env,
                    stdout=output,
                    stderr=output,
                    timeout=1200,
                )
        finally:
            if server:
                stop(server)
    counts = [
        dict(n.attrib)
        for n in ET.parse(evidence / "suite.xml").getroot().iter("testsuite")
    ]
    manifest = {
        "exit_code": result.returncode,
        "mirror": str(mirror),
        "selection": selection,
        "source_count": len(pins),
        "databases": names,
        "counts": counts,
        "command": cmd,
    }
    # Preserve mirror-local fixture outputs, never private profiles.
    shutil.copytree(mirror / "evidence", evidence / "legacy-output")
    manifest["evidence_hashes"] = {
        p.relative_to(evidence).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in evidence.rglob("*")
        if p.is_file()
    }
    (evidence / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({"evidence": str(evidence), **manifest}, indent=2))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
