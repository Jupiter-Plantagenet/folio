"""Local live supervisor for the retained Folio workspace (loopback only).

  python scripts/live_supervisor.py run [--adopt] # foreground supervisor (start detached)
  python scripts/live_supervisor.py status        # PIDs/ports/health/heartbeat, no secrets
  python scripts/live_supervisor.py stop          # stops ONLY the trees it recorded
  python scripts/live_supervisor.py pause-model  --lock-owner <owner>  # marker + stop ONLY the model tree
  python scripts/live_supervisor.py resume-model --lock-owner <owner>  # remove marker; loop restarts model
  python scripts/live_supervisor.py watchdog            # one check: restart a dead/hung supervisor
  python scripts/live_supervisor.py watchdog-install    # Windows scheduled task, every minute
  python scripts/live_supervisor.py watchdog-uninstall

pause-model/resume-model require that the caller holds the shared build lock
C:/Users/user/portfolio-build/.cache/product-build.lock (owner name recorded in the lock).
While .local/live-round4/model.paused exists the run loop never starts/restarts the model,
/health reports "model":"paused" and agent runs fail closed with MODEL_PAUSED.

Round 6 hardening (the round-4 supervisor died silently when C: hit 0 bytes free and an
unguarded state write raised ENOSPC):
* every state write is guarded (retry, non-atomic fallback, never fatal); every loop
  iteration is guarded; any exception (and fatal exit) is logged to supervisor.log;
* the supervisor writes a heartbeat (heartbeat_utc) every 5 s; `status` reports its age;
* the crash-restart cap is time-windowed: at most 5 restarts per component per 10 min.
  When the window is full the component is marked RESTART_CAP_REACHED (escalation in
  state.json and supervisor.log) and is restarted again once the window allows;
* `run --adopt` adopts still-running recorded components (PID + create time + port
  ownership verified) instead of refusing their ports, so a replacement supervisor takes
  over orphans without downtime;
* `watchdog` (run every minute by a scheduled task) restarts a dead or hung supervisor
  with `run --adopt`, unless an operator `stop` left the supervisor.stopped marker;
* the worker writes a heartbeat file that API /health reports (FOLIO_WORKER_HEARTBEAT).

Components: target 8201, API 8101, receipt/analysis worker, export worker loop,
Vite UI 5101, compiled staging 8111, local model 18301. Database principals come
from the private .local/database-profiles.json 'browser' profile (never printed);
schema mode is always 'runtime'. Refuses occupied ports it does not own; never kills
unrelated processes. State/logs: .local/live-round4/ (private, not served).
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))  # application package for in-process maintenance
sys.path.insert(0, str(ROOT.parent))  # shared tools/product_lock.py for the host-wide lock
STATE = ROOT / ".local/live-round4"
MAINTENANCE = ROOT / ".local/maintenance/folio-quiesce.json"
PY = str(ROOT / ".venv/Scripts/python.exe")
PYW = str(ROOT / ".venv/Scripts/pythonw.exe")
NODE = "C:/Program Files/nodejs/node.exe"
EXPORT_ROOT = "C:\\Users\\user\\.portfolio-local-access\\folio_exports_c1dcb5f7b06541f9_artifacts"
MARKER = STATE / "model.paused"
STOPPED = STATE / "supervisor.stopped"
LOG = STATE / "supervisor.log"
LOCK = ROOT.parent / ".cache/product-build.lock"
PORTS = {"target": 8201, "api": 8101, "ui": 5101, "staging": 8111, "model": 18301}
RESTART_LIMIT = 5
RESTART_WINDOW_S = 600
HEARTBEAT_S = 5
HUNG_S = 120  # supervisor heartbeat older than this while its PID lives = hung
DISK_LOW_BYTES = 1024**3
WATCHDOG_TASK = "FolioSupervisorWatchdog"
RETENTION_INTERVAL_S = 3600
BACKUP_EXPIRY_INTERVAL_S = 86400
PAIRED_BACKUP_INTERVAL_S = 86400
PAIRED_BACKUP_RETRY_S = 300
PAIRED_BACKUP_DRAIN_TIMEOUT_S = 1200
MAINTENANCE_LOCK_KEY = "folio-paired-backup-admission-v1"
# Host-RAM prompt cache of llama-server (round 6): validated output-identical on the dev
# set, frees ~8 GiB of private memory. See docs/parallel-delivery-round6-runtime.md.
MODEL_CACHE_RAM_MIB = "0"


def utc():
    return datetime.now(timezone.utc).isoformat()


def log(event, **fields):
    """Append one JSON line to supervisor.log. Never raises (disk may be full)."""
    try:
        with LOG.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"at": utc(), "pid": os.getpid(), "event": event, **fields}) + "\n")
    except OSError:
        pass


def acquire_retention_lock(purpose):
    """Take the shared product lock atomically; never wait or steal it."""
    from tools.product_lock import acquire

    return acquire("folio", purpose, LOCK)


def release_retention_lock():
    from tools.product_lock import release

    return release(LOCK)


def maintenance_active():
    try:
        info = MAINTENANCE.lstat()
        return MAINTENANCE.is_file() and not MAINTENANCE.is_symlink() and not (getattr(info, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False
    except OSError:
        return True


def create_maintenance_marker(run_id):
    from folio.private_storage import secure_private_directory, secure_private_file

    secure_private_directory(MAINTENANCE.parent)
    payload = {"schema": "folio-maintenance-v1", "run_id": run_id,
               "created_utc": utc(), "purpose": "paired-backup", "pid": os.getpid()}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with MAINTENANCE.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    secure_private_file(MAINTENANCE, inherited=True)
    return hashlib.sha256(raw).hexdigest()


def remove_maintenance_marker(expected_hash):
    if not maintenance_active():
        return False
    if MAINTENANCE.is_symlink():
        raise RuntimeError("MAINTENANCE_MARKER_REPARSE_POINT")
    raw = MAINTENANCE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise RuntimeError("MAINTENANCE_MARKER_OWNERSHIP_MISMATCH")
    MAINTENANCE.unlink()
    return True


def acquire_database_claim_barrier(dsn):
    """Try the session-exclusive claim gate; callers retry without blocking loop."""
    import psycopg

    db = psycopg.connect(dsn, autocommit=True, application_name="folio-paired-backup-barrier")
    held = db.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                      (MAINTENANCE_LOCK_KEY,)).fetchone()[0]
    if not held:
        db.close()
        return None
    return db


def maintenance_health(port):
    import httpx

    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
        if response.status_code != 200:
            return None
        body = response.json()
        if body.get("maintenance") is not True:
            return None
        count = body.get("active_requests")
        return count if type(count) is int and count >= 0 else None
    except (httpx.HTTPError, ValueError, TypeError):
        return None


def backup_child_finished(child, timeout_s):
    """Return a completed status; never terminate a capture process on timeout."""
    code = child.poll()
    if code is not None:
        return code, False
    if time.monotonic() - child.started_monotonic > timeout_s:
        return None, True
    return None, False


def begin_paired_backup(table, state):
    """Create the maintenance marker/barrier; return a context or a safe defer."""
    paired = state.setdefault("paired_backup", {})
    if maintenance_active():
        paired["last_error"] = "MAINTENANCE_MARKER_RECOVERY_REQUIRED"
        return None
    if not acquire_retention_lock("paired-backup"):
        paired.update(last_deferred_utc=utc(), last_error="SHARED_PRODUCT_LOCK_ACTIVE")
        log("paired_backup_deferred", reason="SHARED_PRODUCT_LOCK_ACTIVE")
        return None
    marker_hash = None
    barrier = None
    try:
        from scripts.paired_backup import resource_preflight

        admission = resource_preflight()
        paired["resource_preflight"] = admission
        if not admission["admitted"]:
            paired.update(last_deferred_utc=utc(), last_error=admission["reason"])
            log("paired_backup_deferred", reason=admission["reason"])
            release_retention_lock()
            return None
        run_id = uuid.uuid4().hex
        marker_hash = create_maintenance_marker(run_id)
        barrier = acquire_database_claim_barrier(table["api"][1]["FOLIO_DSN"])
        context = {
            "run_id": run_id,
            "marker_hash": marker_hash,
            "barrier": barrier,
            "started_monotonic": time.monotonic(),
            "child": None,
            "child_log": None,
            "stamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "host_lock": True,
            "deadline_alarm_logged": False,
        }
        paired.update(last_attempt_utc=utc(), run_id=run_id,
                      phase="claim_barrier_pending" if barrier is None else "draining")
        log("paired_backup_started", run_id=run_id,
            claim_barrier="held" if barrier is not None else "pending")
        return context
    except Exception as error:
        if barrier is not None:
            barrier.close()
        if marker_hash:
            try:
                remove_maintenance_marker(marker_hash)
            except Exception:
                paired["last_error"] = "MAINTENANCE_CLEANUP_FAILED"
                log("paired_backup_cleanup_failed", error="MAINTENANCE_CLEANUP_FAILED")
                release_retention_lock()
                return None
        paired.update(last_failure_utc=utc(), last_error=type(error).__name__)
        release_retention_lock()
        log("paired_backup_failed", error=type(error).__name__)
        return None


def _start_paired_backup_child(context, table):
    from folio.private_storage import secure_private_directory, secure_private_file

    secure_private_directory(STATE)
    log_path = STATE / "paired-backup.log"
    log_path.touch(exist_ok=True)
    secure_private_file(log_path)
    stream = log_path.open("ab")
    argv = [PY, "-B", str(ROOT / "scripts/paired_backup.py"), "--once",
            "--stamp", context["stamp"]]
    env = {**table["api"][1], "FOLIO_MAINTENANCE_MARKER": str(MAINTENANCE)}
    child = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    child.started_monotonic = time.monotonic()
    context["child"], context["child_log"] = child, stream


def finish_paired_backup(context, state, *, code=None, error=None):
    """Release only this run's barrier/marker/lock; never terminate a live child."""
    paired = state.setdefault("paired_backup", {})
    cleanup_error = None
    if context.get("child_log"):
        context["child_log"].close()
    barrier = context.get("barrier")
    if barrier is not None:
        try:
            barrier.close()
        except Exception:
            cleanup_error = "CLAIM_BARRIER_RELEASE_FAILED"
    try:
        if not remove_maintenance_marker(context["marker_hash"]):
            cleanup_error = "MAINTENANCE_MARKER_MISSING"
    except Exception:
        cleanup_error = "MAINTENANCE_CLEANUP_FAILED"
    if context.get("host_lock"):
        try:
            if not release_retention_lock():
                cleanup_error = cleanup_error or "SHARED_PRODUCT_LOCK_RELEASE_FAILED"
        except Exception:
            cleanup_error = cleanup_error or "SHARED_PRODUCT_LOCK_RELEASE_FAILED"
    if cleanup_error:
        paired.update(last_failure_utc=utc(), last_error=cleanup_error,
                      phase="operator_recovery_required")
        log("paired_backup_cleanup_failed", error=cleanup_error)
        return
    if code == 0 and error is None:
        paired.update(last_success_utc=utc(), last_error=None, phase="idle",
                      last_result={"status": "paired_backup_captured", "run_id": context["run_id"],
                                   "backup_id": context["stamp"]})
        log("paired_backup_succeeded", run_id=context["run_id"], backup_id=context["stamp"])
    else:
        paired.update(last_failure_utc=utc(), last_error=error or "PAIRED_BACKUP_CAPTURE_FAILED",
                      phase="idle")
        log("paired_backup_failed", run_id=context["run_id"],
            error=error or "PAIRED_BACKUP_CAPTURE_FAILED", exit_code=code)


SELECTED_UI_BUILD = None


def ui_build():
    """Select a validated local build without following reparse points."""
    global SELECTED_UI_BUILD
    from folio.private_storage import _validate_existing_ancestors

    cache = ROOT / "web/.cache"
    _validate_existing_ancestors(cache)
    cache_meta = cache.lstat()
    if not cache.is_dir() or cache.is_symlink() or getattr(cache_meta, "st_file_attributes", 0) & 0x400:
        raise SystemExit("UI_BUILD_CACHE_INVALID")
    explicit = os.environ.get("FOLIO_UI_BUILD_DIR")
    if explicit:
        candidate = Path(os.path.abspath(explicit))
        expected = Path(os.path.abspath(cache))
        if candidate.parent != expected or not candidate.name.startswith("application-build-"):
            raise SystemExit("UI_BUILD_OVERRIDE_INVALID")
        candidates = [candidate]
    else:
        candidates = list(cache.glob("application-build-*"))
    verified = []
    for candidate in candidates:
        try:
            _validate_existing_ancestors(candidate)
            metadata = candidate.lstat()
            _validate_existing_ancestors(candidate / "index.html")
            if (candidate.is_dir() and not candidate.is_symlink()
                    and not (getattr(metadata, "st_file_attributes", 0) & 0x400)
                    and (candidate / "index.html").is_file()):
                verified.append(candidate)
        except (OSError, RuntimeError):
            continue
    if explicit:
        if len(verified) != 1:
            raise SystemExit("UI_BUILD_OVERRIDE_UNAVAILABLE")
        selected = verified[0]
    else:
        if not verified:
            raise SystemExit("VERIFIED_UI_BUILD_REQUIRED")
        selected = max(verified, key=lambda item: item.lstat().st_mtime)
    SELECTED_UI_BUILD = str(selected)
    return selected


def base_env():
    profiles = json.loads((ROOT / ".local/database-profiles.json").read_text())
    env = {
        k: os.environ[k]
        for k in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PROGRAMFILES", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME")
        if k in os.environ
    }
    env.update(profiles["browser"])
    tmp = str(STATE / "tmp")
    env.update(
        FOLIO_SCHEMA_MODE="runtime",
        FOLIO_EXPORT_ROOT=EXPORT_ROOT,
        FOLIO_TARGET_URL="http://127.0.0.1:8201",
        FOLIO_MODEL_ALIAS="folio-local",
        FOLIO_MODEL_PROFILE="local-qwen3-8b-q4km-llamacpp-b10964-v1",
        FOLIO_MODEL_PAUSE_MARKER=str(MARKER),
        FOLIO_MODEL_CACHE_RAM_MIB=MODEL_CACHE_RAM_MIB,
        FOLIO_WORKER_HEARTBEAT=str(STATE / "worker.heartbeat.json"),
        FOLIO_MAINTENANCE_MARKER=str(MAINTENANCE),
        PYTHONPATH=str(ROOT),
        PYTHONUTF8="1",
        PYTHONUNBUFFERED="1",
        PYTHONDONTWRITEBYTECODE="1",
        TMP=tmp, TEMP=tmp, TMPDIR=tmp,
    )
    return env


def specs():
    env = base_env()
    staging_env = {**env, "FOLIO_UI_ORIGIN": "http://127.0.0.1:8111"}
    ui_env = {**env, "FOLIO_UI_PORT": "5101", "FOLIO_API_URL": "http://127.0.0.1:8101"}
    for k in ("FOLIO_DSN", "FOLIO_TARGET_DSN", "FOLIO_TARGET_READ_DSN"):
        ui_env.pop(k, None)  # the dev server never needs database principals
    uv = [PY, "-B", "-m", "uvicorn"]
    return {
        "model": ([PY, "-B", "scripts/model_serve.py"], env, ROOT, None),
        "target": (uv + ["folio.target:app", "--host", "127.0.0.1", "--port", "8201", "--no-access-log"], env, ROOT, None),
        "api": (uv + ["folio.workbench:app", "--host", "127.0.0.1", "--port", "8101", "--no-access-log"], env, ROOT, None),
        "worker": ([PY, "-B", "-m", "folio.worker", "--loop"], env, ROOT, None),
        "exportworker": ([PY, "-B", "-m", "folio.exportworker", "--once"], env, ROOT, 3.0),
        "ui": ([NODE, "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "5101", "--strictPort"], ui_env, ROOT / "web", None),
        "staging": ([PY, "-I", "-B", "-X", "utf8", str(ROOT / "serve_local.py"), "--serve-child", "--ui-dir", str(ui_build()), "--port", "8111"], staging_env, ROOT, None),
    }


def listening(port):
    import socket

    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def ready(port, path="/health", timeout=90):
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}{path}", timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def write_state(state):
    """Persist state.json. Never fatal: ENOSPC or a sharing violation is logged and retried.

    The round-4 supervisor died here (ENOSPC while C: had 0 bytes free).
    """
    state["heartbeat_utc"] = utc()
    payload = json.dumps(state, indent=2)
    error = None
    for _ in range(3):
        try:
            tmp = STATE / "state.json.tmp"
            tmp.write_text(payload)
            tmp.replace(STATE / "state.json")
            return True
        except OSError as e:  # ENOSPC, or WinError 5/32 while a reader holds the file
            error = e
            time.sleep(0.2)
    try:  # non-atomic fallback (readers tolerate a retry)
        (STATE / "state.json").write_text(payload)
        return True
    except OSError as e:
        error = e
    log("state_write_failed", error=type(error).__name__, errno=getattr(error, "errno", None))
    return False


def read_state():
    for _ in range(5):
        try:
            return json.loads((STATE / "state.json").read_text())
        except (OSError, ValueError):
            time.sleep(0.2)
    return json.loads((STATE / "state.json").read_text())


def matches(pid, created):
    import psutil

    try:
        return bool(pid) and abs(psutil.Process(pid).create_time() - created) <= 0.01
    except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
        return False


def tree_pids(pid):
    import psutil

    try:
        p = psutil.Process(pid)
        return {pid} | {c.pid for c in p.children(recursive=True)}
    except psutil.NoSuchProcess:
        return set()


def port_owner_pids(port):
    import psutil

    return {
        c.pid
        for c in psutil.net_connections(kind="tcp")
        if c.status == "LISTEN" and c.laddr and c.laddr.port == port and c.pid
    }


class Adopted:
    """Popen-like handle for a still-running component recorded by a previous supervisor."""

    def __init__(self, pid, created):
        self.pid, self.created, self.returncode = pid, created, None

    def poll(self):
        if self.returncode is None and not matches(self.pid, self.created):
            self.returncode = -1  # exit code of a non-child is unknowable
        return self.returncode


def restart_allowed(comp, now):
    """Time-windowed cap: at most RESTART_LIMIT restarts per RESTART_WINDOW_S."""
    times = [t for t in comp.get("restart_times", []) if now - t < RESTART_WINDOW_S]
    comp["restart_times"] = times
    if len(times) < RESTART_LIMIT:
        if comp.pop("escalation", None):
            log("restart_cap_window_reopened", component=comp.get("name"))
        comp.pop("suppressed_until_epoch", None)
        return True
    if comp.get("escalation") != "RESTART_CAP_REACHED":
        comp["escalation"] = "RESTART_CAP_REACHED"
        comp["suppressed_until_epoch"] = times[0] + RESTART_WINDOW_S
        log("restart_cap_reached", component=comp.get("name"), restarts_in_window=len(times),
            window_s=RESTART_WINDOW_S, retry_after_epoch=comp["suppressed_until_epoch"])
    return False


def run():
    import psutil

    adopt = "--adopt" in sys.argv
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "tmp").mkdir(exist_ok=True)
    STOPPED.unlink(missing_ok=True)
    previous = {}
    previous_retention = {}
    previous_backup_retention = {}
    previous_paired_backup = {}
    if (STATE / "state.json").exists():
        try:
            prior_state = read_state()
            if adopt:
                previous = prior_state.get("components", {})
            previous_retention = prior_state.get("retention", {})
            previous_backup_retention = prior_state.get("backup_retention", {})
            previous_paired_backup = prior_state.get("paired_backup", {})
        except (OSError, ValueError):
            previous = {}
    procs, adopted = {}, []
    for name, c in previous.items():
        if name == "exportworker" or not matches(c.get("pid"), c.get("created")):
            continue
        port = PORTS.get(name)
        if port and listening(port) and not (port_owner_pids(port) & tree_pids(c["pid"])):
            continue  # someone else owns that port now; do not adopt
        procs[name] = Adopted(c["pid"], c["created"])
        adopted.append(name)
    busy = [n for n, p in PORTS.items() if n not in procs and listening(p)]
    if busy:
        log("refused_ports_occupied", ports=busy)
        raise SystemExit("PORTS_OCCUPIED:" + ",".join(busy) + " (nothing stopped)")
    table = specs()
    state = {
        "supervisor_pid": os.getpid(),
        "selected_ui_build": SELECTED_UI_BUILD or str(ui_build()),
        "supervisor_created": psutil.Process().create_time(),
        "started_utc": utc(),
        "adopted": adopted,
        "restart_policy": {"limit": RESTART_LIMIT, "window_s": RESTART_WINDOW_S},
        "components": {n: {**previous[n], "name": n, "adopted_utc": utc()} for n in adopted},
        "retention": previous_retention,
        "backup_retention": previous_backup_retention,
        "paired_backup": previous_paired_backup,
    }
    log("supervisor_started", adopted=adopted)

    def start(name, persist=True):
        argv, env, cwd, _ = table[name]
        with (STATE / f"{name}.log").open("ab") as out:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        procs[name] = p
        old = state["components"].get(name, {})
        try:
            created = psutil.Process(p.pid).create_time()
        except psutil.NoSuchProcess:
            created = None
        state["components"][name] = {
            **{k: old[k] for k in ("restart_times", "last_exit") if k in old},
            "name": name, "pid": p.pid, "created": created,
            "argv0": Path(argv[0]).name, "port": PORTS.get(name),
            "started_utc": utc(),
            "restarts": old.get("restarts", -1) + 1,
        }
        if persist:
            write_state(state)

    order = [("model", 18301, "/health", 240), ("target", 8201, "/health", 60),
             ("api", 8101, "/health", 60), ("staging", 8111, "/health", 60),
             ("ui", 5101, "/", 60)]
    # Explicit operator mode for an offline evaluation memory window. Stop the
    # normal supervisor first; ordinary `run` continues to start the full app.
    model_only = '--model-only' in sys.argv
    if model_only and any(name != 'model' for name in procs):
        raise SystemExit('QUIESCE_APPLICATION_BEFORE_MODEL_ONLY')
    state['mode'] = 'model-only' if model_only else 'application'
    if model_only:
        order = order[:1]
    for name, port, path, timeout in order:
        if name in procs:
            continue
        if name == "model" and MARKER.exists():
            state["components"]["model"] = {"name": "model", "pid": None, "created": None, "argv0": None,
                                            "port": port, "paused": True, "restarts": 0}
            write_state(state)
            continue
        start(name)
        state["components"][name]["ready"] = ready(port, path, timeout)
        write_state(state)
    if not model_only and "worker" not in procs:
        start("worker")
    write_state(state)
    last_export = 0.0
    last_retention = 0.0
    last_backup_expiry = 0.0
    next_paired_backup_attempt = 0.0
    paired_backup_context = None
    prior_capture = state["paired_backup"].get("last_success_utc")
    if prior_capture:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(prior_capture)).total_seconds()
            next_paired_backup_attempt = time.monotonic() + max(0, PAIRED_BACKUP_INTERVAL_S - age)
        except (TypeError, ValueError):
            state["paired_backup"]["last_error"] = "LAST_SUCCESS_TIMESTAMP_INVALID"
    if maintenance_active():
        state["paired_backup"]["last_error"] = "MAINTENANCE_MARKER_RECOVERY_REQUIRED"
    last_beat = time.monotonic()
    disk_low = False
    while True:
        try:
            if MARKER.exists():
                if not state["components"]["model"].get("paused"):
                    state["components"]["model"]["paused"] = True
                    write_state(state)
            elif state["components"]["model"].get("paused"):
                # Operator resume: start again without consuming the crash-restart budget.
                keep = {k: state["components"]["model"].get(k) for k in ("restarts", "restart_times")}
                start("model")
                state["components"]["model"].update({k: v for k, v in keep.items() if v is not None})
                state["components"]["model"]["paused"] = False
                state["components"]["model"]["resumed_utc"] = utc()
                write_state(state)
                log("model_resumed")
            now = time.time()
            for name in ("model", "target", "api", "staging", "ui", "worker"):
                if model_only and name != 'model':
                    continue
                maintenance = maintenance_active()
                if name == "model" and (MARKER.exists() or name not in procs):
                    continue
                if name == "worker" and name in procs and maintenance:
                    if procs[name].poll() is not None:
                        state["components"].setdefault(name, {})["maintenance_paused"] = True
                    continue
                if name == "worker" and name in procs and state["components"].get(name, {}).get("maintenance_paused"):
                    if procs[name].poll() is None:
                        continue
                    old = state["components"][name]
                    restart_count, restart_times = old.get("restarts", 0), old.get("restart_times", [])
                    start(name)
                    state["components"][name]["restarts"] = restart_count
                    if restart_times:
                        state["components"][name]["restart_times"] = restart_times
                    state["components"][name].pop("maintenance_paused", None)
                    log("worker_resumed_after_backup")
                    continue
                if procs[name].poll() is None:
                    continue
                comp = state["components"][name]
                if comp.get("pid") == procs[name].pid and "exited_utc" not in comp:
                    comp["last_exit"] = procs[name].returncode
                    comp["exited_utc"] = utc()
                    log("component_exited", component=name, code=procs[name].returncode)
                    write_state(state)
                if restart_allowed(comp, now):
                    comp["restart_times"].append(now)
                    comp.pop("exited_utc", None)
                    start(name)
                    log("component_restarted", component=name,
                        restarts_in_window=len(state["components"][name]["restart_times"]))
                else:
                    write_state(state)
            p = procs.get("exportworker")
            if maintenance_active():
                if p is not None and p.poll() is not None:
                    state.setdefault("components", {}).setdefault("exportworker", {})["maintenance_paused"] = True
            elif not model_only and p is not None and state["components"].get("exportworker", {}).get("maintenance_paused"):
                if p.poll() is not None:
                    old = state["components"]["exportworker"]
                    last_exit = p.returncode
                    start("exportworker", persist=False)
                    state["components"]["exportworker"].update(restarts=old.get("restarts", 0),
                                                               last_exit=last_exit)
                    state["components"]["exportworker"].pop("maintenance_paused", None)
                    last_export = time.monotonic()
                    log("exportworker_resumed_after_backup")
            elif not model_only and (p is None or p.poll() is not None) and time.monotonic() - last_export >= table["exportworker"][3]:
                last_exit = p.returncode if p is not None else None
                last_export = time.monotonic()
                start("exportworker", persist=False)  # every 3 s: persisted by the heartbeat
                state["components"]["exportworker"].update(restarts=0, last_exit=last_exit)
            # Daily capture keeps a marker, a host lock and a DB claim barrier
            # while owned workers exit at idle. The UI/API remain alive for reads;
            # the maintenance middleware blocks database-backed requests.
            if not model_only and paired_backup_context is None and time.monotonic() >= next_paired_backup_attempt:
                if maintenance_active():
                    state.setdefault("paired_backup", {}).update(
                        last_error="MAINTENANCE_MARKER_RECOVERY_REQUIRED",
                        last_deferred_utc=utc(),
                    )
                    next_paired_backup_attempt = time.monotonic() + PAIRED_BACKUP_RETRY_S
                else:
                    paired_backup_context = begin_paired_backup(table, state)
                    next_paired_backup_attempt = time.monotonic() + PAIRED_BACKUP_RETRY_S
            if paired_backup_context is not None:
                context = paired_backup_context
                paired = state.setdefault("paired_backup", {})
                elapsed = time.monotonic() - context["started_monotonic"]
                if context["barrier"] is None:
                    context["barrier"] = acquire_database_claim_barrier(table["api"][1]["FOLIO_DSN"])
                    if context["barrier"] is not None:
                        paired["phase"] = "draining"
                        log("paired_backup_claim_barrier_acquired", run_id=context["run_id"])
                if context["child"] is None and elapsed > PAIRED_BACKUP_DRAIN_TIMEOUT_S:
                    finish_paired_backup(context, state, error="PAIRED_BACKUP_DRAIN_TIMEOUT")
                    paired_backup_context = None
                    next_paired_backup_attempt = time.monotonic() + PAIRED_BACKUP_RETRY_S
                elif context["child"] is None and context["barrier"] is not None:
                    api_active = maintenance_health(PORTS["api"])
                    target_active = maintenance_health(PORTS["target"])
                    worker = procs.get("worker")
                    exporter = procs.get("exportworker")
                    worker_idle = worker is None or worker.poll() is not None
                    exporter_idle = exporter is None or exporter.poll() is not None
                    if api_active == 0 and target_active == 0 and worker_idle and exporter_idle:
                        try:
                            _start_paired_backup_child(context, table)
                            paired["phase"] = "capturing"
                            paired["child_pid"] = context["child"].pid
                            log("paired_backup_capture_started", run_id=context["run_id"])
                        except Exception as error:
                            finish_paired_backup(context, state, error=type(error).__name__)
                            paired_backup_context = None
                            next_paired_backup_attempt = time.monotonic() + PAIRED_BACKUP_RETRY_S
                elif context["child"] is not None:
                    code, exceeded = backup_child_finished(context["child"], 1800)
                    if exceeded and not context["deadline_alarm_logged"]:
                        context["deadline_alarm_logged"] = True
                        paired.update(last_error="PAIRED_BACKUP_CAPTURE_OVERDUE",
                                      last_overdue_utc=utc())
                        log("paired_backup_capture_overdue", run_id=context["run_id"])
                    if code is not None:
                        finish_paired_backup(
                            context, state, code=code,
                            error=None if code == 0 else "PAIRED_BACKUP_CAPTURE_FAILED",
                        )
                        paired_backup_context = None
                        next_paired_backup_attempt = time.monotonic() + (
                            PAIRED_BACKUP_INTERVAL_S if code == 0 else PAIRED_BACKUP_RETRY_S
                        )
            # Retention is a normal-application-only supervised job. It runs in
            # the supervisor process with the existing scoped runtime profile,
            # never as a child carrying a database environment. Shared product
            # work locks defer it visibly; no queue/process is reset or killed.
            if (not model_only and not maintenance_active() and paired_backup_context is None
                    and time.monotonic() - last_retention >= RETENTION_INTERVAL_S):
                last_retention = time.monotonic()
                retention = state.setdefault("retention", {})
                retention["last_attempt_utc"] = utc()
                lock_acquired = False
                try:
                    lock_acquired = acquire_retention_lock("retention-maintenance")
                    if not lock_acquired:
                        retention["last_deferred_utc"] = utc()
                        retention["last_error"] = "SHARED_PRODUCT_LOCK_ACTIVE"
                        log("retention_deferred", reason="SHARED_PRODUCT_LOCK_ACTIVE")
                    else:
                        previous_dsn = os.environ.get("FOLIO_DSN")
                        os.environ["FOLIO_DSN"] = table["api"][1]["FOLIO_DSN"]
                        try:
                            from folio.retention_backend import run_retention_tick
                            result = run_retention_tick()
                        finally:
                            if previous_dsn is None:
                                os.environ.pop("FOLIO_DSN", None)
                            else:
                                os.environ["FOLIO_DSN"] = previous_dsn
                        retention.update(last_success_utc=utc(), last_error=None,
                                         last_result=result)
                        log("retention_succeeded", **result)
                except Exception as e:  # noqa: BLE001 - retained as health alarm
                    retention["last_error"] = type(e).__name__
                    retention["last_failure_utc"] = utc()
                    log("retention_failed", error=type(e).__name__)
                finally:
                    if lock_acquired:
                        release_retention_lock()
            if (not model_only and not maintenance_active() and paired_backup_context is None
                    and time.monotonic() - last_backup_expiry >= BACKUP_EXPIRY_INTERVAL_S):
                last_backup_expiry = time.monotonic()
                backup_retention = state.setdefault("backup_retention", {})
                backup_retention["last_attempt_utc"] = utc()
                lock_acquired = False
                try:
                    lock_acquired = acquire_retention_lock("backup-retention")
                    if not lock_acquired:
                        backup_retention["last_deferred_utc"] = utc()
                        backup_retention["last_error"] = "SHARED_PRODUCT_LOCK_ACTIVE"
                        log("backup_expiry_deferred", reason="SHARED_PRODUCT_LOCK_ACTIVE")
                    else:
                        from scripts.paired_backup import resource_preflight
                        admission = resource_preflight()
                        if not admission["admitted"]:
                            backup_retention.update(last_error=admission["reason"],
                                                    last_deferred_utc=utc(),
                                                    resource_preflight=admission)
                            log("backup_expiry_deferred", reason=admission["reason"])
                        else:
                            from scripts.backup_retention import archive_expired
                            result = archive_expired()
                            backup_retention.update(last_success_utc=utc(), last_error=None,
                                                    last_result=result,
                                                    resource_preflight=admission)
                            log("backup_expiry_succeeded", **result)
                except Exception as e:  # noqa: BLE001 - retained as health alarm
                    backup_retention["last_error"] = type(e).__name__
                    backup_retention["last_failure_utc"] = utc()
                    log("backup_expiry_failed", error=type(e).__name__)
                finally:
                    if lock_acquired:
                        release_retention_lock()
            if time.monotonic() - last_beat >= HEARTBEAT_S:
                last_beat = time.monotonic()
                free = shutil.disk_usage(STATE).free
                if (free < DISK_LOW_BYTES) != disk_low:
                    disk_low = free < DISK_LOW_BYTES
                    log("disk_low" if disk_low else "disk_ok", free_bytes=free)
                state["disk_low"] = disk_low
                write_state(state)
        except Exception as e:  # noqa: BLE001 - never die silently; log and keep supervising
            log("loop_error", error=type(e).__name__)
        time.sleep(1)


def require_lock_holder():
    owner = sys.argv[sys.argv.index("--lock-owner") + 1] if "--lock-owner" in sys.argv else None
    held = " ".join(p.read_text(errors="replace") for p in LOCK.glob("*")) if LOCK.is_dir() else ""
    if not owner or not LOCK.is_dir() or owner not in held:
        raise SystemExit("BUILD_LOCK_HOLDER_REQUIRED: take " + str(LOCK) + " (atomic mkdir, record owner) "
                         "and pass --lock-owner <owner>; nothing changed")


def pause_model():
    require_lock_holder()
    state = read_state()
    MARKER.write_text(json.dumps({"paused_utc": utc(), "by": sys.argv[-1]}))
    c = state["components"].get("model", {})
    result = tree_kill(c["pid"], c["created"]) if c.get("pid") else "not-running"
    log("model_paused", by=sys.argv[-1], result=result)
    print(json.dumps({"marker": str(MARKER), "model": result}))


def resume_model():
    require_lock_holder()
    MARKER.unlink(missing_ok=True)
    print(json.dumps({"marker_removed": True, "note": "run loop restarts the model within ~1 s; poll status"}))


def tree_kill(pid, created):
    import psutil

    try:
        p = psutil.Process(pid)
        if abs(p.create_time() - created) > 0.01:
            return "pid-reused-skip"
        for c in p.children(recursive=True):
            c.kill()
        p.kill()
        return "killed"
    except psutil.NoSuchProcess:
        return "gone"


def stop():
    STOPPED.write_text(json.dumps({"stopped_utc": utc()}))  # watchdog must not resurrect
    state = read_state()
    out = {"supervisor": tree_kill(state["supervisor_pid"], state["supervisor_created"])}
    for name, c in state["components"].items():
        out[name] = tree_kill(c["pid"], c["created"]) if c.get("pid") else "not-running"
    log("operator_stop", result=out)
    print(json.dumps(out, indent=2))


def supervisor_health(state):
    alive = matches(state.get("supervisor_pid"), state.get("supervisor_created"))
    beat = state.get("heartbeat_utc")
    age = None
    if beat:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(beat)).total_seconds()
    return alive, age


def status():
    import httpx
    import psutil

    state = read_state()
    alive, age = supervisor_health(state)
    out = {"supervisor_pid": state["supervisor_pid"], "supervisor_alive": alive,
           "supervisor_heartbeat_age_s": None if age is None else round(age, 1),
           "supervisor_hung": bool(alive and age is not None and age > HUNG_S),
           "operator_stopped": STOPPED.exists(),
           "model_paused": MARKER.exists(), "disk_low": state.get("disk_low"),
           "watchdog_task": watchdog_installed(), "components": {}}
    retention = state.get("retention", {})
    last_success = retention.get("last_success_utc")
    lag = None
    if last_success:
        lag = (datetime.now(timezone.utc) - datetime.fromisoformat(last_success)).total_seconds()
    result = retention.get("last_result", {})
    decisions_due = (result.get("decision_text", {}).get("eligible_case_decisions_remaining", 0)
                     if isinstance(result, dict) else 0)
    history_result = result.get("decision_history", {}) if isinstance(result, dict) else {}
    history_due = (history_result.get("eligible_events_remaining", 0) +
                   history_result.get("eligible_proposals_remaining", 0)
                   if isinstance(history_result, dict) else 0)
    scan_truncated = (history_result.get("event_scan_truncated", False) or
                      history_result.get("proposal_scan_truncated", False)
                      if isinstance(history_result, dict) else False)
    deferred_overflow = (history_result.get("deferred_retry_overflow", False)
                         if isinstance(history_result, dict) else False)
    out["retention"] = {**retention, "success_lag_s": None if lag is None else round(lag, 1),
                        "eligible_due_text_remaining": decisions_due + history_due,
                        "scan_truncated": scan_truncated,
                        "deferred_retry_overflow": deferred_overflow,
                        "alarm": bool(retention.get("last_error") or lag is None or
                                      lag > 2 * RETENTION_INTERVAL_S or decisions_due or
                                      history_due or deferred_overflow)}
    backup_retention = state.get("backup_retention", {})
    backup_success = backup_retention.get("last_success_utc")
    backup_lag = None
    if backup_success:
        backup_lag = (datetime.now(timezone.utc) - datetime.fromisoformat(backup_success)).total_seconds()
    out["backup_retention"] = {
        **backup_retention,
        "success_lag_s": None if backup_lag is None else round(backup_lag, 1),
        "alarm": bool(backup_retention.get("last_error") or backup_lag is None or
                      backup_lag > 2 * BACKUP_EXPIRY_INTERVAL_S or
                      not (backup_retention.get("last_result") or {}).get("inventory_count")),
    }
    paired = state.get("paired_backup", {})
    capture = paired.get("last_success_utc")
    capture_lag = None
    if capture:
        capture_lag = (datetime.now(timezone.utc) - datetime.fromisoformat(capture)).total_seconds()
    out["paired_backup"] = {
        **paired,
        "success_lag_s": None if capture_lag is None else round(capture_lag, 1),
        "alarm": bool(paired.get("last_error") or capture_lag is None or
                      capture_lag > PAIRED_BACKUP_INTERVAL_S),
    }
    for name, c in state["components"].items():
        alive = bool(c.get("pid")) and psutil.pid_exists(c["pid"])
        entry = {"pid": c["pid"], "alive": alive, "port": c.get("port"), "restarts": c.get("restarts"),
                 "restarts_in_window": len([t for t in c.get("restart_times", []) if time.time() - t < RESTART_WINDOW_S])}
        if c.get("escalation"):
            entry["escalation"] = c["escalation"]
        if c.get("port"):
            path = "/" if name == "ui" else "/health"
            try:
                entry["http"] = httpx.get(f"http://127.0.0.1:{c['port']}{path}", timeout=3).status_code
            except httpx.HTTPError as e:
                entry["http"] = type(e).__name__
        out["components"][name] = entry
    print(json.dumps(out, indent=2))


def start_detached(argv):
    """Start a process outside any caller job object (WMI Win32_Process.Create)."""
    quoted = " ".join('"' + a + '"' if " " in a else a for a in argv)
    command = (
        "$r = Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{CommandLine='"
        + quoted.replace("'", "''") + "'; CurrentDirectory='" + str(ROOT) + "'};"
        " Write-Output $r.ProcessId"
    )
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=60,
                          creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()


def watchdog():
    """One idempotent check (scheduled every minute). Restarts a dead or hung supervisor."""
    import psutil

    if STOPPED.exists():
        return print(json.dumps({"watchdog": "operator_stopped_noop"}))
    starting = STATE / "watchdog.starting"
    try:
        if starting.exists() and time.time() - starting.stat().st_mtime < 300:
            return print(json.dumps({"watchdog": "start_in_progress_noop"}))
    except OSError:
        pass
    try:
        state = read_state()
    except (OSError, ValueError):
        state = {}
    alive, age = supervisor_health(state) if state else (False, None)
    try:  # startup can wait up to 240 s for the model without a state write
        booting = (datetime.now(timezone.utc) - datetime.fromisoformat(state["started_utc"])).total_seconds() < 600
    except (KeyError, TypeError, ValueError):
        booting = False
    if alive and (age is None or age <= HUNG_S or booting):
        return print(json.dumps({"watchdog": "healthy", "heartbeat_age_s": age}))
    if alive:  # hung: kill ONLY the supervisor process; its components are adopted
        try:
            psutil.Process(state["supervisor_pid"]).kill()
        except psutil.NoSuchProcess:
            pass
        log("watchdog_killed_hung_supervisor", heartbeat_age_s=age)
    if shutil.disk_usage(STATE).free < 256 * 1024**2:
        log("watchdog_deferred_disk_full")
        return print(json.dumps({"watchdog": "deferred_disk_full"}))
    try:
        starting.write_text(utc())
    except OSError:
        pass
    pid = start_detached([PY, "-B", str(ROOT / "scripts/live_supervisor.py"), "run", "--adopt"])
    log("watchdog_restarted_supervisor", previous_pid=state.get("supervisor_pid"),
        reason="hung" if alive else "dead", new_launcher_pid=pid)
    print(json.dumps({"watchdog": "restarted", "launcher_pid": pid}))


def watchdog_installed():
    r = subprocess.run(["schtasks", "/Query", "/TN", WATCHDOG_TASK], capture_output=True, text=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return r.returncode == 0


def watchdog_install():
    action = f'"{PYW}" -B "{ROOT / "scripts/live_supervisor.py"}" watchdog'
    r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "1", "/TN", WATCHDOG_TASK,
                        "/TR", action], capture_output=True, text=True)
    log("watchdog_install", returncode=r.returncode)
    print(json.dumps({"task": WATCHDOG_TASK, "returncode": r.returncode, "stdout": r.stdout.strip(),
                      "stderr": r.stderr.strip()}))


def watchdog_uninstall():
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", WATCHDOG_TASK], capture_output=True, text=True)
    print(json.dumps({"task": WATCHDOG_TASK, "returncode": r.returncode}))


def main():
    command = sys.argv[1]
    handler = {"run": run, "stop": stop, "status": status, "pause-model": pause_model,
               "resume-model": resume_model, "watchdog": watchdog,
               "watchdog-install": watchdog_install, "watchdog-uninstall": watchdog_uninstall}[command]
    if command != "run":
        return handler()
    try:
        handler()
    except BaseException as e:  # record every supervisor exit, including SystemExit
        log("supervisor_exit", error=type(e).__name__)
        raise


if __name__ == "__main__":
    main()
