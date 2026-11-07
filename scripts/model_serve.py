"""Product-owned local model runtime: Qwen3-8B Q4_K_M GGUF on llama.cpp CUDA.

Loopback 18301 only, 8192 context, one decode slot, no provider credentials.
Verifies the pinned weights SHA256 (publisher Qwen/Qwen3-8B-GGUF @ 7c41481f,
Apache-2.0) before launch; refuses an occupied port; never stops other apps.
Runs in the foreground (the caller decides detachment); Ctrl+C stops it.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# llama.cpp CUDA build 10964 server binary; FOLIO_LLAMA_SERVER overrides the build-host path.
ENGINE = Path(
    os.environ.get(
        "FOLIO_LLAMA_SERVER",
        "C:/Users/user/AppData/Local/hermes/tools/llamacpp-cuda-10964-win32-x64/llama-server.exe",
    )
)
MODEL_DIR = ROOT / ".cache/models/qwen3-8b-gguf-7c41481f"
WEIGHTS = MODEL_DIR / "Qwen3-8B-Q4_K_M.gguf"
SHA = "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
SIZE = 5027783488
ALIAS = "folio-local"
CACHE_RAM = os.getenv("FOLIO_MODEL_CACHE_RAM_MIB", "0").strip()  # round 6 default
# Bound temporary prompt-processing buffers without shrinking the full context,
# changing weights/KV precision, or increasing the one-slot concurrency.
BATCH_SIZE = 512
UBATCH_SIZE = 128
# File-backed loading avoids committing a second anonymous weight copy on
# Windows. Admission still checks both available physical RAM and commit charge.
LOAD_MODE = os.getenv("FOLIO_MODEL_LOAD_MODE", "mmap").strip()
if LOAD_MODE not in {"none", "mmap", "auto"}:
    raise SystemExit("FOLIO_MODEL_LOAD_MODE must be none, mmap, or auto")
if CACHE_RAM and not CACHE_RAM.lstrip("-").isdigit():
    raise SystemExit("FOLIO_MODEL_CACHE_RAM_MIB must be an integer number of MiB")
PROFILE = {
    "profile": "local-qwen3-8b-q4km-llamacpp-b10964-v1",
    "publisher_repo": "Qwen/Qwen3-8B-GGUF",
    "revision": "7c41481f57cb95916b40956ab2f0b139b296d974",
    "file": WEIGHTS.name,
    "sha256": SHA,
    "license": "apache-2.0",
    "engine": "llama.cpp build 10964 (b29c606e2) CUDA",
    "context": 8192,
    "decode_slots": 1,
    "logical_batch_tokens": BATCH_SIZE,
    "physical_batch_tokens": UBATCH_SIZE,
    "kv_cache": "q8_0",
    "host_prompt_cache_mib": CACHE_RAM or "default(8192)",
    "weight_load_mode": LOAD_MODE,
    "thinking": "disabled via chat_template_kwargs.enable_thinking=false",
}


def command(port=18301):
    return [
        str(ENGINE), "--model", str(WEIGHTS), "--alias", ALIAS,
        "--host", "127.0.0.1", "--port", str(port),
        "--ctx-size", "8192", "--parallel", "1", "--threads", "4",
        "--batch-size", str(BATCH_SIZE), "--ubatch-size", str(UBATCH_SIZE),
        "--n-gpu-layers", "99", "--flash-attn", "on",
        # File-backed mapping avoids the anonymous host weight copy on Windows.
        "--load-mode", LOAD_MODE,
        "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
        "--jinja", "--metrics", "--no-webui",
        # Host-RAM prompt cache cap in MiB (llama.cpp default 8192). Round 6 validated
        # 0 (disabled) as output-identical on the dev set; the supervisor passes it.
        *(["--cache-ram", CACHE_RAM] if CACHE_RAM else []),
    ]


# Host RAM/commit/disk admission is report-only unless FOLIO_RESOURCE_GATES=enforce
# (user direction 2026-11-04). Functional needs such as free VRAM still block.
def resource_gate(ok, code):
    if ok:
        return
    if os.environ.get("FOLIO_RESOURCE_GATES") == "enforce":
        raise SystemExit(code)
    print(json.dumps({"resource_gate": code, "mode": "report-only"}), flush=True)


def main():
    import socket

    if WEIGHTS.stat().st_size != SIZE:
        raise SystemExit("WEIGHTS_SIZE_MISMATCH")
    with WEIGHTS.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != SHA:
            raise SystemExit("WEIGHTS_INTEGRITY_FAILED")
    resource_gate(shutil.disk_usage(ROOT).free >= 50 * 1024**3, "DISK_ADMISSION_DENIED")
    import psutil

    resource_gate(psutil.virtual_memory().available >= 6 * 1024**3, "RAM_ADMISSION_DENIED")
    try:
        commit = json.loads(subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory | "
             "Select-Object CommittedBytes,CommitLimit | ConvertTo-Json -Compress"],
            text=True, timeout=30))
        commit_ok = (
            int(commit["CommitLimit"]) > 0
            and int(commit["CommittedBytes"]) / int(commit["CommitLimit"]) < .85
        )
    except (subprocess.SubprocessError, OSError, ValueError, KeyError):
        # A slow or failed probe is "unknown": it blocks only in enforce mode.
        commit_ok = False
    resource_gate(commit_ok, "COMMIT_ADMISSION_DENIED")
    gpu = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if int(gpu.splitlines()[0]) < 5800:
        raise SystemExit("VRAM_ADMISSION_DENIED: need 5800 MiB free; nothing stopped")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 18301)) == 0:
            raise SystemExit("MODEL_PORT_OCCUPIED; nothing stopped")
    (MODEL_DIR / "profile.json").write_text(json.dumps(PROFILE, indent=2))
    sys.stdout.flush()
    return subprocess.call(command())


if __name__ == "__main__":
    raise SystemExit(main())
