"""Product-owned staged runtime; never changes Hermes configuration or processes."""

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from runtime import ROOT, is_free, ready, stop

BASE = ROOT / "evidence/documents/model/local-smoke"
ENGINE = Path(
    "C:/Users/user/AppData/Local/hermes/tools/llamacpp-cuda-10964-win32-x64/llama-server.exe"
)
WEIGHTS = Path(
    "C:/Users/user/AppData/Local/hermes/models/Spark-X2.6-4B-Abliterated-Q5_K_M.gguf"
)
SHA = "9e59dcdf6fa6b18cbf146f1b2f5ae47ba30fdcb21e6903b327fc726d050460a1"
REV = "59e1a0bd4133693f084901c53162dcdca2375761"


class Memory(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
        (n, ctypes.c_ulonglong)
        for n in [
            "total",
            "available",
            "page_total",
            "page_available",
            "virtual_total",
            "virtual_available",
            "extended",
        ]
    ]


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    m = Memory()
    m.length = ctypes.sizeof(m)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        raise RuntimeError("Memory probe failed")
    gpu = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    free_vram = int(gpu.split(",")[-1])
    preflight = {
        "ram_total_bytes": m.total,
        "ram_available_bytes": m.available,
        "disk_free_bytes": shutil.disk_usage(ROOT).free,
        "gpu": gpu,
        "admission": {
            "ram_available_min_bytes": 4 * 1024**3,
            "vram_free_min_mib": 4400,
            "disk_free_min_bytes": 10 * 1024**3,
        },
    }
    (BASE / "preflight.json").write_text(json.dumps(preflight, indent=2))
    # Host RAM/disk are report-only unless FOLIO_RESOURCE_GATES=enforce
    # (user direction 2026-11-04); free VRAM is a functional need and still blocks.
    enforce = os.environ.get("FOLIO_RESOURCE_GATES") == "enforce"
    if free_vram < 4400 or (
        enforce
        and (m.available < 4 * 1024**3 or shutil.disk_usage(ROOT).free < 10 * 1024**3)
    ):
        raise SystemExit(
            "RESOURCE_ADMISSION_DENIED; no process loaded or unrelated application stopped"
        )
    if not is_free(18301):
        raise SystemExit("Model port occupied; no process stopped")
    h = hashlib.file_digest(WEIGHTS.open("rb"), "sha256").hexdigest()
    if h != SHA or WEIGHTS.stat().st_size != 2977897600:
        raise SystemExit("WEIGHTS_INTEGRITY_FAILED")
    sources = {
        "derivative-card.md": f"https://huggingface.co/alexwkleung/Spark-X2.6-4B-Abliterated-GGUF/raw/{REV}/README.md",
        "derivative-LICENSE.txt": f"https://huggingface.co/alexwkleung/Spark-X2.6-4B-Abliterated-GGUF/raw/{REV}/LICENSE",
        "base-card.md": "https://huggingface.co/XHToken/Spark-X2.6-4B/raw/main/README.md",
        "llama-server-doc.md": "https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/README.md",
    }
    for name, url in sources.items():
        (BASE / name).write_bytes(urllib.request.urlopen(url, timeout=30).read())
    version = subprocess.run(
        [str(ENGINE), "--version"], capture_output=True, text=True, check=True
    )
    (BASE / "engine-version.txt").write_text(version.stdout + version.stderr)
    command = [
        str(ENGINE),
        "--model",
        str(WEIGHTS),
        "--alias",
        "folio-spark",
        "--host",
        "127.0.0.1",
        "--port",
        "18301",
        "--ctx-size",
        "8192",
        "--batch-size",
        "128",
        "--ubatch-size",
        "128",
        "--parallel",
        "1",
        "--threads",
        "4",
        "--n-gpu-layers",
        "99",
        "--flash-attn",
        "on",
        "--jinja",
        "--no-webui",
        "--offline",
        "--log-verbosity",
        "5",
    ]
    manifest = {
        "command": command,
        "weights_sha256": h,
        "weights_bytes": WEIGHTS.stat().st_size,
        "repo_revision": REV,
        "engine_sha256": hashlib.file_digest(ENGINE.open("rb"), "sha256").hexdigest(),
        "sources": sources,
        "purpose": "single authored packet real-model tool-loop smoke; not held-out quality gate",
        "hermes_configuration_modified": False,
    }
    (BASE / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2))
    with (BASE / "server.log").open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, cwd=ROOT)
        try:
            ready(18301)
            if "--serve" in sys.argv:
                print(
                    "Folio local model runtime ready at loopback 18301; Ctrl+C stops only this owned runtime.",
                    flush=True,
                )
                while process.poll() is None:
                    time.sleep(1)
                return process.returncode
            (BASE / "gpu-after-load.txt").write_text(
                subprocess.check_output(["nvidia-smi"], text=True)
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/isolated_tests.py",
                    "tests/test_model_live.py",
                ],
                env={**os.environ, "FOLIO_REAL_MODEL_TEST": "1"},
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            (BASE / "test.log").write_text(
                result.stdout + result.stderr, encoding="utf-8"
            )
            print(result.stdout + result.stderr)
            if result.returncode == 0:
                browser = subprocess.run(
                    [
                        sys.executable,
                        "scripts/isolated_browser.py",
                        "agent-live.spec.ts",
                    ],
                    env={**os.environ, "FOLIO_REAL_MODEL_TEST": "1"},
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                (BASE / "browser-test.log").write_text(
                    browser.stdout + browser.stderr, encoding="utf-8"
                )
                print(browser.stdout + browser.stderr)
                return browser.returncode
            return result.returncode
        finally:
            stop(process)
            (BASE / "cleanup.json").write_text(
                json.dumps(
                    {
                        "owned_pid": process.pid,
                        "exit_code": process.poll(),
                        "port_free": is_free(18301),
                    }
                )
            )


if __name__ == "__main__":
    raise SystemExit(main())
