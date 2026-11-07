"""Unique isolated export/backend proofs. Never imports a retained runtime profile."""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.path.abspath(__file__)).parents[1]
MAX_PUBLIC_BATCH_BYTES = 32 * 1024 * 1024
MAX_PUBLIC_BATCH_FILES = 512
MAX_PUBLIC_FINDINGS = 8192
TEST_SELECTOR = re.compile(
    r"^tests/test_[A-Za-z0-9_]+\.py(?:::[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)?)?$"
)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def main():
    from secret_scan import (
        make_safe_directory,
        validate_c_ancestry,
        write_bytes_exclusive,
    )

    validate_c_ancestry(ROOT)
    selection = validate_selection(sys.argv[1:])
    from isolated_database import create_profile
    from runtime import is_free, ready, stop

    if not is_free(18201) or not is_free(18291):
        raise SystemExit("Proof ports occupied; nothing stopped")
    stamp = "export" + str(time.time_ns())
    evidence = ROOT / "evidence/documents/exports" / stamp
    make_safe_directory(evidence)
    # The mirror is the tests' ROOT, so they write .local/ (disposable-role DSNs,
    # identity keys) under it. Keep it private and gitignored; only scanned test
    # outputs are copied into evidence after the run (round-6 credential fix).
    mirror = ROOT / ".local/export-proof-mirrors" / stamp / "source-mirror"
    make_safe_directory(mirror)
    private_outputs = mirror.parent / "test-outputs"
    make_safe_directory(private_outputs)
    # Existing regression fixtures intentionally use fixed paths; only mirror-local dirs.
    make_safe_directory(mirror / "evidence/documents/receipt-concurrency/fix-cycle-2")
    make_safe_directory(mirror / "evidence/documents/proof")
    pins = {}
    from secret_scan import copy_file_exclusive, iter_files, validate_c_ancestry

    for folder in (
        "folio",
        "tests",
        "scripts",
        "ops",
        "contracts",
        "fixtures",
        "target-ledger",
    ):
        source_root = ROOT / folder
        validate_c_ancestry(source_root)
        for src in iter_files([source_root], text_only=False):
            if "__pycache__" not in src.parts:
                rel = src.relative_to(ROOT)
                dest = mirror / rel
                make_safe_directory(dest.parent)
                validate_c_ancestry(src)
                pins[rel.as_posix()] = copy_file_exclusive(src, dest)
    for name in ("pytest.ini", "ruff.toml", "requirements.lock"):
        copy_file_exclusive(ROOT / name, mirror / name)
    write_bytes_exclusive(
        evidence / "source-pins.json", json.dumps(pins, indent=2).encode("utf-8")
    )
    profile_root = ROOT / ".local/isolated" / stamp
    make_safe_directory(profile_root)
    make_safe_directory(profile_root / "artifacts")
    names, env = create_profile(ROOT, stamp)
    from folio.identity import provision

    env = {
        **os.environ,
        **env,
        **provision(ROOT / ".local/isolated" / stamp),
        "PYTHONPATH": str(mirror) + os.pathsep + str(mirror / "scripts"),
        "FOLIO_SCHEMA_MODE": "bootstrap",
        "FOLIO_TARGET_URL": "http://127.0.0.1:18201",
        "FOLIO_PROOF_PROXY_PORT": "18291",
        "FOLIO_EXPORT_PROOF": "1",
        "FOLIO_EXPORT_ROOT": str(ROOT / ".local/isolated" / stamp / "artifacts"),
        # Per-run disposable operator credential; never the live one, never in source.
        "FOLIO_TARGET_OPERATOR_PASSWORD": __import__("secrets").token_urlsafe(24),
        "FOLIO_EXPORT_EVIDENCE": str(private_outputs),
        "TMPDIR": str(mirror.parent / "tmp"),
        "FOLIO_REAL_MODEL_TEST": "0",
    }
    make_safe_directory(Path(env["TMPDIR"]))
    with (private_outputs / "target.log").open("x", encoding="utf-8") as log:
        p = subprocess.Popen(
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
        try:
            ready(18201)
            cmd = [
                sys.executable,
                "-m",
                "pytest",
                *selection,
                "-v",
                "--junitxml=" + str(private_outputs / "suite.xml"),
            ]
            r = subprocess.run(cmd, cwd=mirror, env=env, capture_output=True, text=True)
            write_bytes_exclusive(
                private_outputs / "pytest.log",
                (r.stdout + r.stderr).encode("utf-8"),
            )
            stop(p)
            log.flush()
            publish_outputs(private_outputs, evidence)
            publish_mirror_evidence(mirror, evidence)
            write_bytes_exclusive(
                evidence / "manifest.json",
                json.dumps(
                    {
                        "databases": names,
                        **public_selection_metadata(selection),
                        "exit_code": r.returncode,
                        "evidence": str(evidence),
                        "command_template": (
                            "python -m pytest <validated test selectors> -v "
                            "--junitxml=<private output path>"
                        ),
                    },
                    indent=2,
                ).encode("utf-8"),
            )
            emit_child_summary(r.returncode, r.stdout, r.stderr, evidence)
            return r.returncode
        finally:
            stop(p)


def publish_mirror_evidence(mirror, evidence):
    """Copy the tests' evidence outputs (never mirror/.local) and fail closed on secrets."""
    from secret_scan import (
        UnsafeScanPath,
        validate_c_ancestry,
        write_bytes_exclusive,
    )

    produced = mirror / "evidence"
    try:
        validate_c_ancestry(produced)
    except UnsafeScanPath as exc:
        if "COMPONENT_MISSING" in str(exc):
            return
        raise
    publish_outputs(produced, evidence / "mirror-evidence")
    write_bytes_exclusive(
        evidence / "mirror-location.json",
        json.dumps(
            {
                "private_mirror": mirror.relative_to(ROOT).as_posix(),
                "secret_scan": "clean",
            },
            indent=2,
        ).encode("utf-8"),
    )


def publish_outputs(produced, destination):
    """Publish exactly scanned files; skipped private trees never get copied."""
    from secret_scan import (
        MAX_BYTES,
        UnsafeScanPath,
        UnsupportedScanContent,
        iter_files,
        make_safe_directory,
        read_regular_file,
        scan_bytes,
        validate_c_ancestry,
        write_bytes_exclusive,
    )

    payloads = []
    findings = []
    batch_bytes = 0
    batch_files = 0
    produced = validate_c_ancestry(produced)
    destination = Path(os.path.abspath(destination))

    for path in iter_files([produced], text_only=False):
        batch_files += 1
        if batch_files > MAX_PUBLIC_BATCH_FILES:
            findings.append(
                {
                    "path": str(path.relative_to(produced)),
                    "reason": "OUTPUT_BATCH_EXCEEDS_FILE_COUNT_LIMIT",
                }
            )
            break
        remaining_bytes = MAX_PUBLIC_BATCH_BYTES - batch_bytes
        try:
            payload = read_regular_file(
                path, max_bytes=min(MAX_BYTES, remaining_bytes)
            )
        except UnsafeScanPath as exc:
            if "EXCEEDS_LIMIT" in str(exc):
                findings.append(
                    {
                        "path": str(path.relative_to(produced)),
                        "reason": (
                            "OUTPUT_BATCH_EXCEEDS_PUBLICATION_MEMORY_LIMIT"
                            if remaining_bytes < MAX_BYTES
                            else "OUTPUT_EXCEEDS_SECRET_SCAN_LIMIT"
                        ),
                    }
                )
                continue
            raise
        try:
            matches = scan_bytes(path, payload)
        except UnsupportedScanContent as exc:
            findings.append(
                {
                    "path": str(path.relative_to(produced)),
                    "reason": str(exc),
                }
            )
            continue
        if len(findings) + len(matches) > MAX_PUBLIC_FINDINGS:
            findings.append(
                {
                    "path": str(path.relative_to(produced)),
                    "reason": "OUTPUT_BATCH_EXCEEDS_FINDING_LIMIT",
                }
            )
            break
        if batch_bytes + len(payload) > MAX_PUBLIC_BATCH_BYTES:
            findings.append(
                {
                    "path": str(path.relative_to(produced)),
                    "reason": "OUTPUT_BATCH_EXCEEDS_PUBLICATION_MEMORY_LIMIT",
                }
            )
            break
        batch_bytes += len(payload)
        findings.extend(matches)
        payloads.append((path, payload))
    if findings:
        make_safe_directory(destination)
        public_findings = summarize_public_findings(findings)
        write_bytes_exclusive(
            destination / "outputs-withheld.json",
            json.dumps(
                {
                    "reason": "TEST_OUTPUT_NOT_CERTIFIED_FOR_PUBLICATION",
                    "finding_counts": public_findings,
                },
                indent=2,
            ).encode("utf-8"),
        )
        raise SystemExit(
            "TEST_OUTPUT_NOT_CERTIFIED_FOR_PUBLICATION; raw outputs kept private"
        )
    targets = [
        (path, destination / path.relative_to(produced), payload)
        for path, payload in payloads
    ]
    for _, target, _ in targets:
        make_safe_directory(target.parent)
        if ":" in target.name:
            raise UnsafeScanPath("SCAN_DESTINATION_ALTERNATE_STREAM")
        try:
            os.lstat(target)
        except FileNotFoundError:
            continue
        raise UnsafeScanPath("SCAN_DESTINATION_ALREADY_EXISTS")
    for _, target, payload in targets:
        write_bytes_exclusive(target, payload)


def summarize_public_findings(findings):
    """Expose only controlled scanner categories and counts in public reports."""
    from secret_scan import PATTERNS

    pattern_counts = {name: 0 for name in PATTERNS}
    reason_counts = {
        "OUTPUT_BATCH_EXCEEDS_FILE_COUNT_LIMIT": 0,
        "OUTPUT_BATCH_EXCEEDS_FINDING_LIMIT": 0,
        "OUTPUT_BATCH_EXCEEDS_PUBLICATION_MEMORY_LIMIT": 0,
        "OUTPUT_CONTAINS_NUL": 0,
        "OUTPUT_FORMAT_UNSUPPORTED": 0,
        "OUTPUT_HAS_BINARY_CONTROL_BYTES": 0,
        "OUTPUT_MATCH_COUNT_LIMIT": 0,
        "OUTPUT_NOT_STRICT_UTF8": 0,
        "OUTPUT_EXCEEDS_SECRET_SCAN_LIMIT": 0,
        "OTHER_WITHHELD_REASON": 0,
    }
    for finding in findings:
        pattern = finding.get("pattern")
        reason = finding.get("reason")
        if pattern in pattern_counts:
            pattern_counts[pattern] += 1
        elif reason in reason_counts and reason != "OTHER_WITHHELD_REASON":
            reason_counts[reason] += 1
        else:
            reason_counts["OTHER_WITHHELD_REASON"] += 1
    return {
        "secret_patterns": {
            name: count for name, count in pattern_counts.items() if count
        },
        "withheld_reasons": {
            name: count for name, count in reason_counts.items() if count
        },
    }


def emit_child_summary(returncode, stdout, stderr, evidence):
    """Report status and location only; child output may contain ephemeral secrets."""
    del stdout, stderr
    try:
        location = Path(evidence).relative_to(ROOT).as_posix()
    except ValueError:
        location = "private-evidence-path"
    print(f"EXPORT_PROOF exit_code={returncode} evidence={location} raw_output=private")


def validate_selection(arguments):
    """Allow only repository-relative pytest test selectors from the test suite."""
    selection = arguments or ["tests/test_export_jobs.py"]
    if len(selection) > 100 or any(not TEST_SELECTOR.fullmatch(item) for item in selection):
        raise SystemExit("INVALID_TEST_SELECTION")
    return list(selection)


def public_selection_metadata(selection):
    """Publish a count and digest, never caller-supplied selector strings."""
    encoded = json.dumps(selection, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    return {
        "selection_count": len(selection),
        "selection_sha256": hashlib.sha256(encoded).hexdigest(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
