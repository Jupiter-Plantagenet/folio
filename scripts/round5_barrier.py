"""Round-5 fault-injection wrappers: run UNCHANGED product code, pause at a named barrier
so the harness can SIGKILL-equivalent (TerminateProcess) the process mid-operation.

  round5_barrier.py worker <before_fence|before_patch|after_patch> [job_id]
  round5_barrier.py export <after_stage|before_ref>
  round5_barrier.py api <port>          # pauses inside the approval transaction

Instrumentation is by monkeypatching in this test process only; product source is not edited.
Each barrier prints one JSON line {"barrier": ...} before pausing.
"""

import json
import os
import sys
import time


def pause(name):
    print(json.dumps({"barrier": name, "pid": os.getpid(), "t": time.time()}), flush=True)
    time.sleep(3600)


def worker(mode, job=None):
    import httpx

    import folio.worker as w

    if mode == "before_fence":
        original = httpx.Client.post

        def post(self, url, *a, **k):
            if url.endswith("/operation-fences"):
                pause("before_fence")
            return original(self, url, *a, **k)

        httpx.Client.post = post
    elif mode == "before_patch":
        httpx.Client.patch = lambda self, *a, **k: pause("before_patch_after_seal")
    elif mode == "after_patch":
        # Target transaction has committed the PATCH; the workbench has not checkpointed.
        w.check = lambda *a, **k: pause("after_patch_before_checkpoint")
    else:
        raise SystemExit("UNKNOWN_BARRIER")
    j = w.claim(job)
    if not j:
        raise SystemExit("NO_CLAIMABLE_JOB")
    print(json.dumps({"claimed": j["id"], "fence": j["fence"], "attempt": j["attempt"]}), flush=True)
    w.execute(j, os.environ["FOLIO_TARGET_URL"])


def export(mode):
    from folio.exportworker import run_once

    run_once(checkpoint=lambda name: pause(name) if name == mode else None)


def api(port):
    import uvicorn

    import folio.workbench as w

    original = w.recorded

    def recorded(db, req, user, b, result):
        if "/approvals" in req.url.path:
            pause("approval_inside_transaction")
        return original(db, req, user, b, result)

    w.recorded = recorded
    uvicorn.run(w.app, host="127.0.0.1", port=int(port), log_level="warning")


if __name__ == "__main__":
    kind, *rest = sys.argv[1:]
    {"worker": worker, "export": export, "api": api}[kind](*rest)
