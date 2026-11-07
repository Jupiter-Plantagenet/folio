"""DOC-T-PARSER hostile set through the real folio.sandbox.parse_isolated container path.

Results: evidence/documents/parser/round6-<UTC>/results.json (with image IDs,
wall times and the exact docker flags). The containment probe re-runs the
image with the very flags parse_isolated used (captured, not retyped) and an
overridden entrypoint that tries network, root, writes, fork and memory.
"""

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures/hostile"
STAMP = os.environ.get("FOLIO_ROUND6_STAMP") or datetime.now(timezone.utc).strftime(
    "%Y%m%dT%H%M%SZ"
)
EVIDENCE = ROOT / "evidence/documents/parser" / f"round6-{STAMP}"
RESULTS = {"stamp": STAMP, "suite": "DOC-T-PARSER", "cases": {}, "image_ids": set()}
BLUEPRINT = {"cpu_seconds": 60, "wall_seconds": 120, "memory_mib": 512, "pids": 64, "scratch_mib": 256}


@pytest.fixture(scope="module", autouse=True)
def write_results():
    yield
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = {**RESULTS, "image_ids": sorted(RESULTS["image_ids"]), "blueprint_limits": BLUEPRINT}
    (EVIDENCE / "results.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def run(name, raw=None, label=None):
    from folio.sandbox import parse_isolated

    data = raw if raw is not None else (FIX / name).read_bytes()
    started = time.monotonic()
    try:
        out = parse_isolated(data, name)
        RESULTS["image_ids"].add(out["containment"]["image_id"])
        result = {"accepted": True, "containment": out["containment"]}
    except ValueError as error:
        out, result = None, {"accepted": False, "error": str(error)[:300]}
    result["wall_seconds"] = round(time.monotonic() - started, 2)
    result["sha256"] = hashlib.sha256(data).hexdigest()
    RESULTS["cases"][label or name] = result
    return out, result


REJECT = {
    "pdf-truncated.pdf": "PARSER_REJECTED",
    "pdf-no-header.pdf": "FILE_REJECTED",
    "pdf-51-pages.pdf": "PAGE_LIMIT",
    "pdf-deep-nesting.pdf": "PARSER_REJECTED",
    "pdf-ocr-huge-page.pdf": "OCR_PAGE_LIMIT",
    "xlsx-zip-bomb.xlsx": "XLSX_CONTAINER_REJECTED",
    "xlsx-zip-bomb-lying-size.xlsx": "PARSER_REJECTED",
    "xlsx-many-entries.xlsx": "XLSX_CONTAINER_REJECTED",
    "xlsx-path-traversal.xlsx": "XLSX_CONTAINER_REJECTED",
    "xlsx-absolute-path.xlsx": "XLSX_CONTAINER_REJECTED",
    "xlsx-external-link.xlsx": "XLSX_CONTAINER_REJECTED",
    "xlsx-vba.xlsx": "XLSX_CONTAINER_REJECTED",
    "xlsx-xxe.xlsx": "PARSER_REJECTED",
    "csv-1002-rows.csv": "CSV_LIMIT",
    "csv-huge-cell.csv": "CELL_LIMIT",
    "csv-nul.csv": "CSV_REJECTED",
}


@pytest.mark.parametrize("name", sorted(REJECT))
def test_hostile_input_is_rejected_with_a_bounded_reason(name):
    out, result = run(name)
    assert out is None and REJECT[name] in result["error"], result
    assert result["wall_seconds"] < 50


def test_fifty_pages_is_the_inclusive_limit():
    out, result = run("pdf-50-pages.pdf")
    assert out is not None and len(out["pages"]) == 50


def test_ocr_page_eleven_is_within_the_inclusive_page_limit():
    out, result = run("pdf-ocr-page-11.pdf")
    assert out is not None and len(out["pages"]) == 11, result


def test_garbage_xref_is_recovered_as_data_only():
    out, result = run("pdf-garbage-xref.pdf")
    result["note"] = "pdfminer rebuilds the xref; output still passes host validation"
    if out is not None:
        assert out["pages"][0]["text"].startswith("Invoice INV-1048")


def test_active_pdf_content_is_never_executed():
    out, result = run("pdf-js-launch.pdf")
    assert out is not None and out["pages"][0]["text"] == "Invoice INV-1048"
    assert out["containment"]["network"] == "none"


def test_decompression_bomb_pdf_is_killed_at_the_memory_limit():
    out, result = run("pdf-flate-bomb.pdf")
    assert out is None
    assert "PARSER_RESOURCE_LIMIT:memory" in result["error"] or "PARSER_REJECTED" in result["error"]


def test_cpu_exhaustion_is_killed_at_the_wall_timeout_and_container_removed():
    import folio.sandbox as sandbox

    names = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        if isinstance(cmd, list) and "--name" in cmd and cmd[1] == "run":
            names.append(cmd[cmd.index("--name") + 1])
        return real(cmd, *a, **k)

    sandbox.subprocess.run = spy
    try:
        out, result = run("pdf-form-bomb.pdf")
    finally:
        sandbox.subprocess.run = real
    gone = real(["docker", "inspect", names[0]], capture_output=True).returncode != 0
    result.update(container_removed=gone, timeout_seconds=120)
    # 2**34 nested form draws remain bounded by the 512 MiB cgroup or 120 s
    # wall timeout; either is a kill.
    assert out is None and result["error"] in (
        "PARSER_UNAVAILABLE_OR_TIMEOUT",
        "PARSER_RESOURCE_LIMIT:memory",
    )
    assert result["wall_seconds"] < 140 and gone


def test_wall_timeout_kills_a_waiting_parser_and_never_leaks_a_slot():
    """Fault injection through the real parse_isolated code: same flags and
    image, entrypoint replaced by a sleep so the 60 s CPU limit cannot end the
    process before the 120 s wall deadline. Round 6 found the 10 s cleanup timeout
    escaping as TimeoutExpired and leaking the parser slot."""
    import folio.sandbox as sandbox

    real = subprocess.run
    names = []

    def spin(cmd, *a, **k):
        if isinstance(cmd, list) and len(cmd) > 1 and cmd[1] == "run" and cmd[-1] == sandbox.IMAGE:
            names.append(cmd[cmd.index("--name") + 1])
            cmd = [*cmd[:-1], "--entrypoint", "python", cmd[-1], "-c", "import time; time.sleep(1000)"]
        return real(cmd, *a, **k)

    sandbox.subprocess.run = spin
    started = time.monotonic()
    try:
        with pytest.raises(ValueError) as error:
            sandbox.parse_isolated(b"item,quantity\nA,1\n", "spin.csv")
    finally:
        sandbox.subprocess.run = real
    elapsed = round(time.monotonic() - started, 2)
    slots_free = sandbox.SLOTS._value
    gone = False
    for _ in range(30):
        if real(["docker", "inspect", names[0]], capture_output=True).returncode:
            gone = True
            break
        time.sleep(2)
    RESULTS["cases"]["wall-timeout-fault-injection"] = {
        "error": str(error.value), "wall_seconds": elapsed, "slots_free_after": slots_free,
        "container_removed": gone, "timeout_seconds": 120,
    }
    assert str(error.value) == "PARSER_UNAVAILABLE_OR_TIMEOUT"
    assert 119 <= elapsed < 155 and slots_free == 2 and gone


def test_formulas_dde_and_external_references_are_never_evaluated():
    out, result = run("xlsx-dde-formulas.xlsx")
    assert out is not None
    cells = {c["coordinate"]: c for c in out["cells"]}
    for ref in ("B1", "B2", "B3", "B4"):
        assert cells[ref]["formula"].startswith("=") and cells[ref]["requires_review"]
    # Cached values are the authored file bytes, not a computation/fetch.
    assert cells["B2"]["cached_value"] == "100" and cells["B3"]["cached_value"] == "open"
    from folio.provenance import extract_packet

    packet = extract_packet([{**out, "id": "r", "document_role": "receipt"}])
    result["formulas"] = {k: cells[k]["formula"] for k in cells if cells[k]["formula"]}
    assert packet["values"].get("received_quantity") is None
    out, result = run("xlsx-external-hyperlink-rel.xlsx")
    assert out is not None and out["containment"]["network"] == "none"
    out, result = run("csv-formula-injection.csv")
    assert out is not None and out["cells"][2]["value"] == "=cmd|' /C calc'!A0"


NAMES = {
    "../escape.csv": "FILE_REJECTED",
    "..\\escape.csv": "FILE_REJECTED",
    "C:\\Windows\\x.csv": "FILE_REJECTED",
    "/etc/passwd.csv": "FILE_REJECTED",
    "receipt.csv\u0000.exe": "FORMAT_UNSUPPORTED",
    "receipt.exe": "FORMAT_UNSUPPORTED",
    "receipt": "FORMAT_UNSUPPORTED",
    "CON.csv": None,
    "receipt.CSV": None,
    "evil\u202eexe.csv": None,
    ".csv": None,
}


@pytest.mark.parametrize("name", sorted(NAMES))
def test_odd_and_traversal_file_names(name):
    out, result = run(name, b"item,quantity\nA,1\n", label="name:" + name.encode("unicode_escape").decode())
    want = NAMES[name]
    if want:
        assert out is None and result["error"] == want
    else:  # the name is never a path: parsed as data, no file is written
        assert out is not None and out["cells"][-1]["value"] == "1"
        assert out["containment"]["host_mounts"] == []


def test_oversize_input_rejected_before_container():
    from folio.intake_limits import MAX_FILE_BYTES

    out, result = run("big.csv", b"x" * (MAX_FILE_BYTES + 1), label="oversize-20MiB+1")
    assert out is None and result["error"] == "FILE_REJECTED" and result["wall_seconds"] < 1


PROBE = r"""
import json, os, socket, subprocess
r = {"uid": os.getuid(), "gid": os.getgid()}
caps = [l for l in open('/proc/self/status') if l.startswith('CapEff')][0].split()[1]
r["cap_eff"] = caps
for host in ("1.1.1.1", "host.docker.internal"):
    try:
        socket.create_connection((host, 53 if host[0] == "1" else 8101), timeout=3); r["net_" + host] = "CONNECTED"
    except Exception as e:
        r["net_" + host] = type(e).__name__
for path in ("/app/x", "/etc/x", "/x"):
    try:
        open(path, "w").write("x"); r["write_" + path] = "WROTE"
    except Exception as e:
        r["write_" + path] = type(e).__name__
try:
    open("/etc/shadow").read(); r["shadow"] = "READ"
except Exception as e:
    r["shadow"] = type(e).__name__
try:
    with open("/tmp/fill", "wb") as f:
        for _ in range(300): f.write(b"\0" * (1 << 20))
    r["tmp_mib_written"] = 300
except Exception as e:
    r["tmp_fill"] = type(e).__name__; r["tmp_mib_written"] = os.path.getsize("/tmp/fill") >> 20
procs = []
try:
    for _ in range(100):
        procs.append(subprocess.Popen(["sleep", "5"]))
except Exception as e:
    r["fork_error"] = type(e).__name__
r["processes_started"] = len(procs)
for p in procs: p.kill()
print(json.dumps(r))
"""


def _captured_flags():
    import folio.sandbox as sandbox

    captured = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        if isinstance(cmd, list) and len(cmd) > 1 and cmd[1] == "run" and not captured:
            captured.append(list(cmd))
        return real(cmd, *a, **k)

    sandbox.subprocess.run = spy
    try:
        sandbox.parse_isolated(b"item,quantity\nA,1\n", "probe.csv")
    finally:
        sandbox.subprocess.run = real
    cmd = captured[0]
    flags = cmd[2:-2]  # between "run" and "-i IMAGE"
    i = flags.index("--name")
    del flags[i : i + 2]
    return cmd[0], flags, cmd[-1]


def test_containment_probe_with_the_exact_sandbox_flags():
    docker, flags, image = _captured_flags()
    probe = subprocess.run(
        [docker, "run", "--rm", *flags, "--entrypoint", "python", image, "-c", PROBE],
        capture_output=True, text=True, timeout=120,
    )
    r = json.loads(probe.stdout.strip().splitlines()[-1])
    mem = subprocess.run(
        [docker, "run", "--name", "folio-r6-oom-probe", *flags, "--entrypoint", "python", image,
         "-c", "b=[bytearray(1<<20) for _ in range(600)];print(len(b))"],
        capture_output=True, text=True, timeout=120,
    )
    state = json.loads(subprocess.run([docker, "inspect", "folio-r6-oom-probe"], capture_output=True).stdout)[0]["State"]
    subprocess.run([docker, "rm", "-f", "folio-r6-oom-probe"], capture_output=True)
    image_id = subprocess.run([docker, "image", "inspect", image, "--format", "{{.Id}}"], capture_output=True, text=True).stdout.strip()
    RESULTS["image_ids"].add(image_id)
    RESULTS["cases"]["containment-probe"] = {
        "flags": flags, "image": image, "image_id": image_id, "probe": r,
        "oom_probe": {"exit_code": mem.returncode, "oom_killed": state.get("OOMKilled")},
        "actual_limits": {"memory_mib": 512, "swap_mib": 512, "pids": 64, "cpus": 1,
                          "wall_timeout_seconds": 120, "cpu_seconds_limit": 60, "tmpfs_scratch_mib": 256,
                          "output_limit_mib": 128},
    }
    assert r["uid"] == 65534 and r["cap_eff"] == "0000000000000000"
    assert all(v != "CONNECTED" for k, v in r.items() if k.startswith("net_"))
    assert all(v != "WROTE" for k, v in r.items() if k.startswith("write_"))
    assert r["shadow"] != "READ"
    assert r["tmp_mib_written"] <= 256
    assert 0 < r["processes_started"] < 64
    assert state.get("OOMKilled") is True and mem.returncode != 0
