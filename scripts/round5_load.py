"""Round-5 DOC-T-LOAD driver on a DISPOSABLE stack (9101/9201). Never touches retained data.

Blueprint scenario: 1000 packets / 10000 pages, 20 users, 5 active runs, 2 parser slots, 60 min.
  users: 10 readers, 5 reviewer-writers (decisions), 2 approvers (approval = dispatch enqueue),
         1 exporter, 2 importers (10-page image-only PDF -> container OCR; product has 2 parser slots)
  runs : 5 agent-run loops (analysis-runs against the operator's already-loaded local model)
Resource admission: requires 6 GiB available RAM, 50 GiB free C: space and system
commit below 85 percent. Output: evidence/documents/load/<tag>-<stamp>/
  python scripts/round5_load.py [minutes]
Round 6: FOLIO_LOAD_TAG (default round5) names the run; FOLIO_LOAD_RAW_ROOT keeps raw
samples in product-private C: scratch (a gzipped copy goes to evidence); admission
is rechecked every 5 seconds and a failed resource check stops new load;
API /health (queue age, verifier lag, alarms) is sampled every 5 s.
"""

import base64
import gzip
import io
import json
import math
import os
import queue
import random
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import psutil
from round5_stack import API, TARGET, Client, Stack, provision

ROOT = Path(__file__).resolve().parents[1]
GATE_GIB = 6.0
DISK_FLOOR_GIB = 50.0
COMMIT_LIMIT_PERCENT = 85.0
MAX_SAMPLE_RECORDS = 1_000_000


class ResourceAbort(RuntimeError):
    pass


def host_resources():
    """Read system commit, rather than pagefile usage, with a bounded probe."""
    counters = json.loads(subprocess.check_output(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory | "
         "Select-Object CommittedBytes,CommitLimit | ConvertTo-Json -Compress"],
        text=True, timeout=12))
    limit = int(counters["CommitLimit"])
    if limit <= 0:
        raise ValueError("SYSTEM_COMMIT_LIMIT_UNAVAILABLE")
    return {"available_gib": psutil.virtual_memory().available / 2**30,
            "c_free_gib": shutil.disk_usage("C:/").free / 2**30,
            "commit_percent": 100.0 * int(counters["CommittedBytes"]) / limit}


def resources_admitted(snapshot):
    # Host RAM/commit/disk admission is report-only unless FOLIO_RESOURCE_GATES=enforce
    # (user direction 2026-11-04). Functional needs such as free VRAM still block.
    if os.environ.get("FOLIO_RESOURCE_GATES") != "enforce":
        return True
    return (snapshot["available_gib"] >= GATE_GIB
            and snapshot["c_free_gib"] >= DISK_FLOOR_GIB
            and snapshot["commit_percent"] < COMMIT_LIMIT_PERCENT)


def scanned_pdf(n, pages=10):
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    buf = io.BytesIO()
    c = Canvas(buf)
    for p in range(pages):
        img = Image.new("L", (1240, 1754), 255)
        d = ImageDraw.Draw(img)
        lines = [f"LOAD PACKET {n:05d} PAGE {p + 1}", "Invoice INV-L5-%05d" % n, "Supplier Aster Supply Co.",
                 "Invoiced quantity 120", "Unit price 48.00 USD", "Synthetic CC0 authored load fixture"]
        for i, t in enumerate(lines):
            d.text((100, 120 + 60 * i), t, fill=0)
        c.drawImage(ImageReader(img), 0, 0, width=595, height=842)
        c.showPage()
    c.save()
    return buf.getvalue()


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def main():
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    if not math.isfinite(minutes) or not 0 < minutes <= 60:
        raise ValueError("LOAD_DURATION_MUST_BE_FINITE_AND_AT_MOST_60_MINUTES")
    stamp = os.getenv("FOLIO_LOAD_TAG", "round5") + time.strftime("-%Y%m%d%H%M%S", time.gmtime())
    out = ROOT / "evidence/documents/load" / stamp
    out.mkdir(parents=True)
    raw_base = Path(os.getenv("FOLIO_LOAD_RAW_ROOT", str(ROOT / ".local/load-raw")))
    # Reject another drive before resolving or examining it.
    if raw_base.drive.upper() != "C:":
        raise ValueError("LOAD_RAW_ROOT_MUST_BE_ON_C")
    raw_base.resolve().relative_to((ROOT / ".local").resolve())
    raw = raw_base / stamp
    raw.mkdir(parents=True, exist_ok=True)
    admission = {**host_resources(), "gate_gib": GATE_GIB,
                 "disk_floor_gib": DISK_FLOOR_GIB, "commit_limit_percent": COMMIT_LIMIT_PERCENT}
    admission["admitted"] = resources_admitted(admission)
    (out / "admission.json").write_text(json.dumps(admission, indent=2))
    if not admission["admitted"]:
        print(json.dumps({"status": "RESOURCE_ADMISSION_DENIED", **admission, "out": str(out)}))
        return 3
    state = None
    stack = None
    aborted = []
    samples, lock = [], threading.Lock()
    stop = threading.Event()

    def rec(kind, cls, status, secs, extra=None):
        with lock:
            if len(samples) >= MAX_SAMPLE_RECORDS:
                if not aborted:
                    aborted.append({"reason": "SAMPLE_RECORD_LIMIT", "t": time.time()})
                stop.set()
                return
            samples.append({"t": time.time(), "kind": kind, "class": cls, "status": status, "s": secs, **(extra or {})})

    resources = []

    def sampler():
        while not stop.is_set():
            rss = 0
            for key, p in (list(stack.procs.items()) if stack is not None else []):
                try:
                    pr = psutil.Process(p.pid)
                    for x in [pr, *pr.children(recursive=True)]:
                        rss += x.memory_info().rss
                except psutil.Error:
                    pass
            try:
                snapshot = host_resources()
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                aborted.append({"reason": "RESOURCE_PROBE_FAILED", "error_class": type(exc).__name__, "t": time.time()})
                stop.set()
                return
            health = None
            try:
                health = httpx.get(f"http://127.0.0.1:{API}/health", timeout=5).json()
            except (httpx.HTTPError, ValueError):
                pass
            record = {"t": time.time(), "stack_rss_mib": rss / 2**20,
                              "host_available_gib": snapshot["available_gib"],
                              "host_cpu_pct": psutil.cpu_percent(interval=None),
                              "c_free_gib": snapshot["c_free_gib"], "commit_percent": snapshot["commit_percent"],
                              "health": {k: health.get(k) for k in ("pending_jobs", "retrying_jobs", "oldest_job_age_s",
                                         "verifier_lag_s", "unknown_outcome_jobs", "alarms")} if health else None}
            resources.append(record)
            try:
                with (raw / "resources-incremental.jsonl").open("a", encoding="utf-8") as resource_log:
                    resource_log.write(json.dumps(record) + "\n")
            except OSError:
                aborted.append({"reason": "RESOURCE_LOG_UNAVAILABLE", "t": time.time()})
                stop.set()
                return
            if not resources_admitted(snapshot) and not aborted:
                aborted.append({"reason": "HOST_RESOURCE_GATE", **snapshot, "t": time.time()})
                stop.set()  # every load thread checks stop
            stop.wait(5)

    smp = threading.Thread(target=sampler)
    smp.start()

    try:
        state = provision()
        if stop.is_set():
            raise ResourceAbort("HOST_RESOURCE_GATE_DURING_PROVISION")
        stack = Stack(state, raw / "logs")
        stack.start("target")
        stack.start("api")
        assert stack.wait_http(TARGET) and stack.wait_http(API)
        if stop.is_set():
            raise ResourceAbort("HOST_RESOURCE_GATE_DURING_STARTUP")
        # Dataset: 1000 packets x 10 logical pages (one authored 10-page text PDF reused for capacity, CC0)
        seed = """import io,json,uuid
from reportlab.pdfgen.canvas import Canvas
from folio.parser import parse_pdf
from folio.target import seed
from folio.workbench import connect,create_case,WORKSPACE
from pathlib import Path
b=io.BytesIO();c=Canvas(b)
for i in range(10):
  c.drawString(60,750,'Authored synthetic load page '+str(i+1));c.showPage()
c.save();src=parse_pdf(b.getvalue());src['id']=str(uuid.uuid4());src['name']='load.pdf'
fx=[{'id':str(uuid.uuid4()),'name':p.name,**parse_pdf(p.read_bytes())} for p in sorted(Path('fixtures/public').glob('*.pdf'))]
ids=[];runs=[]
with connect() as db:
  for i in range(1000):
    rid=str(uuid.uuid4());seed(WORKSPACE,rid);ids.append(create_case([src],target_id=rid,db=db)['id'])
  for i in range(60):
    rid=str(uuid.uuid4());seed(WORKSPACE,rid);runs.append(create_case(fx,target_id=rid,db=db)['id'])
  writers=[]
  for i in range(400):
    rid=str(uuid.uuid4());seed(WORKSPACE,rid);writers.append(create_case(target_id=rid,db=db)['id'])
print(json.dumps({'cases':ids,'run_cases':runs,'writer_cases':writers,'pages':10*len(ids)}))"""
        t = time.monotonic()
        seed_out, seed_err = raw / "logs/seed.stdout.log", raw / "logs/seed.stderr.log"
        with seed_out.open("xb") as stdout, seed_err.open("xb") as stderr:
            child = subprocess.Popen([sys.executable, "-c", seed], env=stack.env, cwd=ROOT,
                                     stdout=stdout, stderr=stderr)
            try:
                while child.poll() is None:
                    if stop.wait(.25):
                        child.kill()  # Only this directly owned fixture seeder.
                        child.wait(timeout=10)
                        raise ResourceAbort("HOST_RESOURCE_GATE_DURING_SEED")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
        if child.returncode:
            raise subprocess.CalledProcessError(child.returncode, "owned-fixture-seeder")
        if stop.is_set():
            raise ResourceAbort("HOST_RESOURCE_GATE_DURING_SEED")
        if seed_out.stat().st_size > 1024 * 1024:
            raise ValueError("SEED_RECEIPT_TOO_LARGE")
        ds = json.loads(seed_out.read_text(encoding="utf-8").strip().splitlines()[-1])
        seed_s = time.monotonic() - t
        stack.start("worker")
        pdfs = queue.Queue()
        for n in range(80):
            if stop.is_set():
                raise ResourceAbort("HOST_RESOURCE_GATE_DURING_IMPORT_FIXTURES")
            pdfs.put(scanned_pdf(n))
        fresh = queue.Queue()
        # Decision/approval/dispatch writes use source-free fixture cases (receipt-support rules
        # correctly refuse decisions on the 10-page capacity packets, which carry no receipt).
        for cid in ds["writer_cases"]:
            fresh.put(cid)
        to_approve = queue.Queue()
        deadline = time.monotonic() + minutes * 60

        def timed(kind, cls, fn):
            t0 = time.monotonic()
            try:
                r = fn()
                rec(kind, cls, r.status_code, time.monotonic() - t0)
                return r
            except httpx.HTTPError as e:
                rec(kind, cls, type(e).__name__, time.monotonic() - t0)
                return None

        def reader(user):
            c = Client(user)
            while time.monotonic() < deadline and not stop.is_set():
                choice = random.random()
                if choice < 0.35:
                    timed("GET /cases/{id}", "read", lambda: c.get(f"/cases/{random.choice(ds['cases'])}"))
                elif choice < 0.55:
                    timed("GET /cases", "read", lambda: c.get("/cases"))
                elif choice < 0.8:
                    timed("GET /activity", "read", lambda: c.get("/activity"))
                else:
                    timed("GET /auth/session", "read", lambda: c.http.get("/api/v1/auth/session"))
                time.sleep(1.0)

        def writer():
            c = Client("reviewer")
            while time.monotonic() < deadline and not stop.is_set():
                try:
                    cid = fresh.get_nowait()
                except queue.Empty:
                    timed("GET /cases/{id}", "read", lambda: c.get(f"/cases/{random.choice(ds['cases'])}"))
                    time.sleep(2)
                    continue
                case = timed("GET /cases/{id}", "read", lambda: c.get(f"/cases/{cid}"))
                if not case or case.status_code != 200:
                    continue
                body = case.json()
                r = timed("POST decisions", "write", lambda: c.post(
                    f"/cases/{cid}/decisions", dict(mode="partial", quantity="100", reason="Load: receipt supports 100",
                                                    evidence_digest=body["source_digest"]), if_match=f'"case-v{body["version"]}"'))
                if r is not None and r.status_code == 201:
                    to_approve.put(r.json()["proposal"])
                time.sleep(18)

        def approver():
            c = Client("approver")
            while time.monotonic() < deadline and not stop.is_set():
                try:
                    p = to_approve.get(timeout=2)
                except queue.Empty:
                    continue
                timed("POST approvals (dispatch enqueue)", "enqueue", lambda: c.post(
                    f"/proposals/{p['id']}/approvals", dict(intent_digest=p["digest"], confirmation="approve"),
                    if_match='"proposal-v1"'))
                time.sleep(1)

        def exporter():
            c = Client("auditor")
            while time.monotonic() < deadline and not stop.is_set():
                cid = random.choice(ds["run_cases"])
                case = timed("GET /cases/{id}", "read", lambda: c.get(f"/cases/{cid}"))
                if case is not None and case.status_code == 200:
                    timed("POST exports (enqueue)", "enqueue", lambda: c.post(
                        f"/cases/{cid}/exports", dict(case_version=case.json()["version"], include_originals=False,
                                                      redaction_profile="audit-v1")))
                time.sleep(20)

        def importer():
            c = Client("reviewer")
            while time.monotonic() < deadline and not stop.is_set():
                try:
                    raw = pdfs.get_nowait()
                except queue.Empty:
                    break
                timed("POST intakes/files (10-page scan OCR)", "ocr_packet", lambda: c.post(
                    "/intakes/files", {"rights_attestation": "authored-synthetic",
                                       "files": [{"name": "scan.pdf", "document_role": "invoice",
                                                  "base64": base64.b64encode(raw).decode()}]}, timeout=600))
                time.sleep(5)

        def agent_run():
            c = Client("reviewer")
            while time.monotonic() < deadline and not stop.is_set():
                cid = random.choice(ds["run_cases"])
                case = c.get(f"/cases/{cid}")
                if case.status_code != 200:
                    time.sleep(5)
                    continue
                t0 = time.monotonic()
                r = timed("POST analysis-runs (enqueue)", "enqueue", lambda: c.post(
                    f"/cases/{cid}/analysis-runs", {"prepare_proposal": False}, if_match=f'"case-v{case.json()["version"]}"'))
                if r is None or r.status_code != 202:
                    time.sleep(5)
                    continue
                rid, status = r.json()["run_id"], "queued"
                while (time.monotonic() < deadline + 600 and not stop.is_set()
                       and status in ("queued", "running")):
                    time.sleep(1)
                    g = c.get(f"/cases/{cid}/analysis-runs/{rid}")
                    status = g.json().get("status") if g.status_code == 200 else status
                rec("analysis run end-to-end (local model)", "model_packet", status, time.monotonic() - t0)
                time.sleep(10)

        def exportworker():
            while time.monotonic() < deadline and not stop.is_set():
                subprocess.run(stack.argv("exportworker"), cwd=ROOT, env=stack.env, capture_output=True)
                time.sleep(3)

        threads = ([threading.Thread(target=reader, args=(u,)) for u in ["viewer", "auditor", "reviewer", "approver", "admin"] * 2]
                   + [threading.Thread(target=writer) for _ in range(5)] + [threading.Thread(target=approver) for _ in range(2)]
                   + [threading.Thread(target=exporter)] + [threading.Thread(target=importer) for _ in range(2)]
                   + [threading.Thread(target=agent_run) for _ in range(5)] + [threading.Thread(target=exportworker)])
        t_start = time.monotonic()
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        elapsed = time.monotonic() - t_start
        stop.set()
        smp.join()
        with stack.wb() as db:
            jobs = db.execute("SELECT state,count(*) FROM jobs GROUP BY 1").fetchall()
            ages = db.execute("SELECT max(extract(epoch FROM clock_timestamp()-x.at)) FROM (SELECT min(e.at) at FROM events e "
                              "JOIN cases c ON c.id=e.case_id WHERE c.body->>'state' IN ('dispatching','seal_pending','sealed') "
                              "GROUP BY c.id) x").fetchone()[0]
        with stack.target_reader() as db:
            dupes = db.execute("SELECT count(*) FROM (SELECT target_id FROM operations GROUP BY 1 HAVING count(*)>1) x").fetchone()[0]
            ops = db.execute("SELECT count(*) FROM operations").fetchone()[0]
    except ResourceAbort as exc:
        (out / "setup-abort.json").write_text(json.dumps({
            "status": "ABORTED", "reason": str(exc), "aborted": aborted,
            "raw_dir": str(raw), "disposable_databases": state["names"] if state else None}, indent=2))
        return 3
    finally:
        stop.set()
        if stack is not None:
            stack.stop_all()
        smp.join(timeout=25)
    (raw / "samples.json").write_text(json.dumps(samples))
    (raw / "resources.json").write_text(json.dumps(resources))
    (out / "samples.json.gz").write_bytes(gzip.compress(json.dumps(samples).encode()))
    (out / "resources.json.gz").write_bytes(gzip.compress(json.dumps(resources).encode()))
    summary = {"minutes_requested": minutes, "elapsed_s": elapsed, "seed_seconds": seed_s, "dataset": {
        "packets": len(ds["cases"]), "logical_pages": ds["pages"], "agent_run_cases": len(ds["run_cases"]),
        "writer_cases": len(ds["writer_cases"])},
        "aborted": aborted, "raw_dir": str(raw), "users": 20, "agent_run_loops": 5, "parser_slots": 2, "admission": admission, "classes": {}, "kinds": {}}
    for key, field in (("classes", "class"), ("kinds", "kind")):
        groups = {}
        for s in samples:
            groups.setdefault(s[field], []).append(s)
        for g, xs in groups.items():
            lat = [x["s"] for x in xs]
            summary[key][g] = {"n": len(xs), "p50_s": pct(lat, .5), "p95_s": pct(lat, .95), "p99_s": pct(lat, .99),
                               "mean_s": statistics.fmean(lat),
                               "http_5xx": sum(1 for x in xs if isinstance(x["status"], int) and x["status"] >= 500),
                               "transport_errors": sum(1 for x in xs if isinstance(x["status"], str) and x["status"] not in ("completed", "failed", "blocked", "recommended")),
                               "statuses": sorted({str(x["status"]) for x in xs})}
    http = [s for s in samples if isinstance(s["status"], int)]
    summary["http_5xx_rate"] = sum(1 for s in http if s["status"] >= 500) / max(1, len(http))
    summary["resources"] = {"peak_stack_rss_mib": max((r["stack_rss_mib"] for r in resources), default=None),
                            "min_host_available_gib": min((r["host_available_gib"] for r in resources), default=None),
                            "mean_host_cpu_pct": statistics.fmean([r["host_cpu_pct"] for r in resources]) if resources else None}
    summary["integrity"] = {"jobs_by_state": jobs, "target_operations": ops, "records_with_duplicate_operations": dupes,
                            "oldest_inflight_case_age_s": ages}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({"summary": str(out / "summary.json")}))
    return 3 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
