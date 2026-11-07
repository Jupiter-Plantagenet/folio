"""Ephemeral rootless/read-only/networkless parser container, no host mounts."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from uuid import uuid4

from folio.intake_limits import MAX_FILE_BYTES, MAX_PARSER_OUTPUT_BYTES

SLOTS = threading.BoundedSemaphore(2)
# FOLIO_PARSER_IMAGE selects a candidate image for evaluation; production uses folio-parser:1.
IMAGE = os.environ.get("FOLIO_PARSER_IMAGE", "folio-parser:1")


def parse_isolated(raw, name):
    if name.rsplit(".", 1)[-1].lower() not in ("pdf", "csv", "xlsx"):
        raise ValueError("FORMAT_UNSUPPORTED")
    if len(raw) > MAX_FILE_BYTES or "/" in name or "\\" in name:
        raise ValueError("FILE_REJECTED")
    docker = shutil.which("docker")
    if docker is None:
        raise ValueError("PARSER_UNAVAILABLE_OR_TIMEOUT")
    if not SLOTS.acquire(blocking=False):
        raise ValueError("PARSER_CAPACITY_RETRY")
    container = "folio-parser-" + uuid4().hex
    command = [
        docker,
        "run",
        "--name",
        container,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--ulimit",
        "cpu=60:60",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
        "--user",
        "65534:65534",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "-i",
        IMAGE,
    ]
    try:
        # Do not buffer parser output in host RAM. The parser's JSON is capped
        # after completion and the sandbox itself has a 512 MiB memory limit.
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            result = subprocess.run(
                command,
                input=json.dumps(
                    {"name": name, "base64": base64.b64encode(raw).decode()}
                ).encode(),
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=120,
            )
            stdout_file.seek(0)
            stdout = stdout_file.read(MAX_PARSER_OUTPUT_BYTES + 1)
        inspection = subprocess.run(
            [docker, "inspect", container], capture_output=True, check=True
        )
        state = json.loads(inspection.stdout)[0]
        if state["State"].get("OOMKilled"):
            raise ValueError("PARSER_RESOURCE_LIMIT:memory")
        if len(stdout) > MAX_PARSER_OUTPUT_BYTES:
            raise ValueError("PARSER_OUTPUT_LIMIT")
        if result.returncode:
            raise ValueError(
                "PARSER_REJECTED:" + stdout.decode(errors="replace")[:250]
            )
        parsed = json.loads(stdout)
        if parsed.get("sha256") != hashlib.sha256(raw).hexdigest():
            raise ValueError("PARSER_HASH_MISMATCH")
        from folio.parser_output import validate

        validate(parsed, raw)  # container JSON is untrusted too
        host = state["HostConfig"]
        parsed["containment"] = {
            "network": host["NetworkMode"],
            "read_only_root": host["ReadonlyRootfs"],
            "memory_bytes": host["Memory"],
            "memory_swap_bytes": host["MemorySwap"],
            "host_mounts": host.get("Binds") or [],
            "user": state["Config"]["User"],
            "image_id": state["Image"],
            "cap_drop": host["CapDrop"],
            "pids_limit": host["PidsLimit"],
            "cpu_quota": host.get("CpuQuota"),
            "cpu_period": host.get("CpuPeriod"),
            "cpu_seconds_limit": next(
                (
                    limit.get("Hard")
                    for limit in host.get("Ulimits", [])
                    if limit.get("Name") == "cpu"
                ),
                None,
            ),
            "tmpfs": host.get("Tmpfs") or {},
            "wall_seconds_limit": 120,
            "exit_code": state["State"]["ExitCode"],
        }
        return parsed
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError) as error:
        raise ValueError("PARSER_UNAVAILABLE_OR_TIMEOUT") from error
    finally:
        # Removing a CPU-spinning container can take longer than 10 s on
        # Docker Desktop. A cleanup timeout must neither escape as a non-
        # ValueError (HTTP 500) nor leak a parser slot: hand the removal to a
        # detached process and always release the slot.
        try:
            subprocess.run(
                [docker, "rm", "-f", container], capture_output=True, timeout=30
            )
        except (subprocess.TimeoutExpired, OSError):
            try:
                subprocess.Popen(
                    [docker, "rm", "-f", container],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                pass
        finally:
            SLOTS.release()
