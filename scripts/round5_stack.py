"""Round-5 disposable Folio stack for destructive ops drills (never the retained stack).

Ports: API 9101, Northstar target 9201. Fresh folio_finish_* databases and folio_test_*
roles (scripts/isolated_database.py), nonowner RLS runtime principals, private export root
and workload identities under .local/round5/<stamp>/ (private; DSNs are never printed).
No retained profile file is read. Components are killed with psutil.Process.kill(),
which is TerminateProcess on Windows (the SIGKILL equivalent: no handlers, no cleanup).
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
PY = sys.executable
API, TARGET = 9101, 9201
ORIGIN = "http://127.0.0.1:9111"
WORKSPACE = "11111111-1111-4111-8111-111111111111"


def free(port):
    import socket

    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def provision(stamp=None):
    from isolated_database import create_profile

    from folio.database_security import admin, provision_runtime
    from folio.identity import provision as identities
    from folio.schema_journal import journal_sql

    stamp = stamp or "r5" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    private = ROOT / ".local/round5" / stamp
    private.mkdir(parents=True, exist_ok=True)
    names, owner_env = create_profile(ROOT, stamp)
    base = {k: v for k, v in os.environ.items() if not k.startswith("FOLIO_")}
    env = {
        **base,
        **owner_env,
        **identities(private / "identities"),
        "FOLIO_SCHEMA_MODE": "bootstrap",
        "PYTHONPATH": str(ROOT),
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "FOLIO_TARGET_URL": f"http://127.0.0.1:{TARGET}",
        "FOLIO_UI_ORIGIN": ORIGIN,
        "FOLIO_EXPORT_ROOT": str((private / "exports").resolve()),
        "FOLIO_REAL_MODEL_TEST": "0",
    }
    subprocess.run(
        [PY, "-c", "from folio.workbench import migrate;from folio.target import migrate as tm;migrate();tm()"],
        env=env, cwd=ROOT, check=True,
    )
    for kind, database in names.items():
        raw = (ROOT / "ops/migrations" / f"002-{kind}.sql").read_bytes()
        admin("BEGIN;" + journal_sql(2, raw) + "COMMIT;", database)
    runtime = {
        "FOLIO_DSN": provision_runtime(names["workbench"], "workbench"),
        "FOLIO_TARGET_DSN": provision_runtime(names["target"], "target"),
        "FOLIO_TARGET_READ_DSN": provision_runtime(names["target"], "reader"),
        "FOLIO_SCHEMA_MODE": "runtime",
    }
    state = {"stamp": stamp, "names": names, "owner": owner_env, "env": {**env, **runtime}}
    (private / "env.json").write_text(json.dumps(state))
    return state


def load(stamp):
    return json.loads((ROOT / ".local/round5" / stamp / "env.json").read_text())


class Stack:
    def __init__(self, state, logdir):
        self.state, self.env = state, state["env"]
        self.logdir = Path(logdir)
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.procs = {}

    def argv(self, name, extra=()):
        uv = [PY, "-B", "-m", "uvicorn"]
        return {
            "target": uv + ["folio.target:app", "--host", "127.0.0.1", "--port", str(TARGET), "--no-access-log"],
            "api": uv + ["folio.workbench:app", "--host", "127.0.0.1", "--port", str(API), "--no-access-log"],
            "worker": [PY, "-B", "-m", "folio.worker", "--loop", "--target", f"http://127.0.0.1:{TARGET}"],
            "exportworker": [PY, "-B", "-m", "folio.exportworker", "--once"],
            "barrier": [PY, "-B", str(ROOT / "scripts/round5_barrier.py"), *extra],
        }[name]

    def start(self, name, extra=(), key=None):
        key = key or name
        log = (self.logdir / f"{key}.log").open("ab")
        p = subprocess.Popen(self.argv(name, extra), cwd=ROOT, env=self.env, stdout=log, stderr=subprocess.STDOUT)
        self.procs[key] = p
        return p

    def kill(self, key):
        """SIGKILL-equivalent: TerminateProcess the whole recorded tree, no graceful path."""
        p = self.procs.get(key)
        if not p or p.poll() is not None:
            return False
        try:
            proc = psutil.Process(p.pid)
            for c in proc.children(recursive=True):
                c.kill()
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        p.wait(10)
        return True

    def stop_all(self):
        for key in list(self.procs):
            self.kill(key)

    def wait_http(self, port, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        return False

    def wait_log(self, key, needle, timeout=60):
        path = self.logdir / f"{key}.log"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and needle in path.read_text(errors="replace"):
                return True
            time.sleep(0.1)
        return False

    # --- database reads (runtime / reader principals only) -----------------
    def wb(self):
        return psycopg.connect(self.env["FOLIO_DSN"])

    def target_reader(self):
        return psycopg.connect(self.env["FOLIO_TARGET_READ_DSN"])


class Client:
    """Real HTTP session for one existing fixture account."""

    def __init__(self, user):
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{API}", timeout=30)
        r = self.http.post("/api/v1/auth/login", json={"username": user, "password": f"folio-{user}-local"})
        r.raise_for_status()
        self.csrf = r.json()["csrf"]
        self.prefix = f"/api/v1/workspaces/{WORKSPACE}"

    def headers(self, if_match=None, key=None):
        h = {"Origin": ORIGIN, "X-CSRF-Token": self.csrf, "Idempotency-Key": key or str(uuid4())}
        if if_match:
            h["If-Match"] = if_match
        return h

    def get(self, path, **kw):
        return self.http.get(self.prefix + path, **kw)

    def post(self, path, json_body, if_match=None, key=None, timeout=30):
        return self.http.post(self.prefix + path, json=json_body, headers=self.headers(if_match, key), timeout=timeout)


def seed_case(stack):
    """Operator fixture path: one new Northstar payable (rev 7) and one bound case."""
    code = (
        "import json,uuid;from folio.target import seed;from folio.workbench import create_case,WORKSPACE;"
        "rid=str(uuid.uuid4());seed(WORKSPACE,rid);c=create_case(target_id=rid);"
        "print(json.dumps({'case':c['id'],'target':rid,'version':c['version'],'digest':c['source_digest']}))"
    )
    out = subprocess.run([PY, "-c", code], env=stack.env, cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def prepare_and_approve(reviewer, approver, seeded, approval_key=None):
    d = reviewer.post(
        f"/cases/{seeded['case']}/decisions",
        dict(mode="partial", quantity="100", reason="Only confirmed receipt", evidence_digest=seeded["digest"]),
        if_match=f'"case-v{seeded["version"]}"',
    )
    d.raise_for_status()
    proposal = d.json()["proposal"]
    a = approver.post(
        f"/proposals/{proposal['id']}/approvals",
        dict(intent_digest=proposal["digest"], confirmation="approve"),
        if_match='"proposal-v1"', key=approval_key,
    )
    return proposal, a


if __name__ == "__main__":
    if sys.argv[1:] == ["provision"]:
        s = provision()
        print(json.dumps({"stamp": s["stamp"], "databases": s["names"]}))
