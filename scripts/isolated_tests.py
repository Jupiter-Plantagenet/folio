"""Run real proofs in new databases and ports without touching the live profile."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from runtime import ROOT, is_free, ready, stop


def source_pins(destination=None):
    """Pin executable maintained source/fixtures; never traverse evidence/seals."""
    paths = []
    for name in (
        "folio",
        "scripts",
        "ops",
        "tests",
        "eval",
        "target-ledger",
        "web/src",
        "web/export-tests",
    ):
        paths.extend(
            p
            for p in (ROOT / name).rglob("*")
            if p.is_file()
            and not {"__pycache__", ".cache", "node_modules"}.intersection(p.parts)
            and p.suffix != ".pyc"
        )
    paths.extend(
        p
        for name in (
            "serve_local.py",
            "pytest.ini",
            "ruff.toml",
            "requirements.lock",
            "requirements.in",
            "web/package.json",
            "web/package-lock.json",
            "web/vite.config.ts",
        )
        if (p := ROOT / name).is_file()
    )
    result = {}
    for path in sorted(set(paths)):
        relative = path.relative_to(ROOT)
        result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if destination is not None:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return result


def main():
    port = 18201
    if not is_free(port) or not is_free(18291):
        raise SystemExit("Folio proof ports occupied; no process stopped")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    from isolated_database import create_profile

    names, database_env = create_profile(ROOT, stamp)
    evidence = ROOT / "evidence/documents/finish" / stamp
    evidence.mkdir(parents=True)
    pinned = source_pins(ROOT / ".local/isolated" / stamp / "source-snapshot")
    (evidence / "source-sha256.json").write_text(json.dumps(pinned, indent=2))
    from folio.identity import provision

    identities = provision(ROOT / ".local/isolated" / stamp)
    env = {
        **os.environ,
        **identities,
        **database_env,
        "FOLIO_SCHEMA_MODE": "bootstrap",
        "PYTHONPATH": str(ROOT) + os.pathsep + str(ROOT / "scripts"),
        "FOLIO_TARGET_URL": f"http://127.0.0.1:{port}",
        "FOLIO_PROOF_PROXY_PORT": "18291",
        # Per-run private artifact root; export tests otherwise fail closed.
        "FOLIO_EXPORT_ROOT": str(ROOT / ".local/isolated" / stamp / "export-artifacts"),
        # Per-run disposable operator credential; never the live one, never in source.
        "FOLIO_TARGET_OPERATOR_PASSWORD": __import__("secrets").token_urlsafe(24),
    }
    with (evidence / "target.log").open("w") as log:
        p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "folio.target:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            ready(port)
            command = [
                sys.executable,
                "-m",
                "pytest",
                *(sys.argv[1:] or ["tests"]),
                "-v",
                f"--junitxml={evidence / 'suite.xml'}",
            ]
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            (evidence / "pytest.log").write_text(
                result.stdout + result.stderr, encoding="utf-8"
            )
            print(result.stdout + result.stderr)
            manifest = {
                "databases": names,
                "target_port": port,
                "exit_code": result.returncode,
                "retained": True,
                "selection": sys.argv[1:] or ["tests"],
                "evidence": str(evidence),
                "source_sha256_manifest": str(evidence / "source-sha256.json"),
                "source_unchanged_during_run": source_pins() == pinned,
            }
            (evidence / "manifest.json").write_text(json.dumps(manifest, indent=2))
            (ROOT / "evidence/documents/finish/latest.json").write_text(
                json.dumps(manifest, indent=2)
            )
            return result.returncode if manifest["source_unchanged_during_run"] else 2
        finally:
            stop(p)


if __name__ == "__main__":
    raise SystemExit(main())
