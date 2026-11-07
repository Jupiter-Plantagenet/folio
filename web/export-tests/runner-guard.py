"""Tiny real Windows resource proofs; no browser/model/backend startup.
python export-tests/runner-guard.py <fresh-owned-output-directory>
"""

import ctypes as winct
import json
import os
import pathlib
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

SOURCE = pathlib.Path(__file__).with_name("run.py")
ns = dict(
    os=os,
    subprocess=subprocess,
    time=time,
    OWNED_CAP=2147483648,
    HOST_RESERVE=536870912,
    MARGIN=134217728,
)
text = SOURCE.read_text()
exec(
    compile(
        text[text.index("# Native authority") : text.index("sources =")],
        str(SOURCE),
        "exec",
    ),
    ns,
)
# Namespace-shadow regression: a caller's SQL `as c` never changes native helpers.
c = object()
OUTPUT = pathlib.Path(sys.argv[1]).resolve()
OUTPUT.mkdir(exist_ok=False, parents=True)
CAP = 268435456
RESERVE = 536870912
MARGIN = 134217728
ADMISSION = 939524096
samples = []
results = []
cleanups = []
env = {
    **os.environ,
    "TMP": str(OUTPUT),
    "TEMP": str(OUTPUT),
    "TMPDIR": str(OUTPUT),
    "PYTHONDONTWRITEBYTECODE": "1",
}


def admission(label):
    free = ns["available"]()
    item = dict(
        label=label,
        available=free,
        required=ADMISSION,
        cap=CAP,
        reserve=RESERVE,
        margin=MARGIN,
        admitted=free >= ADMISSION,
    )
    with (OUTPUT / "admission.jsonl").open("a") as f:
        f.write(json.dumps(item) + "\n")
    if free < ADMISSION:
        raise RuntimeError("LIGHT NOT ADMITTED; no retry")


def sample(job, label):
    s = job.capture()
    root = ns["memory_on_handle"](ns["wink"].GetCurrentProcess())
    free = ns["available"]()
    rss = s["rss"] + root["rss"]
    samples.append(dict(label=label, job=s, monitor=root, rss=rss, available=free))
    if rss > CAP or free < RESERVE + MARGIN:
        raise RuntimeError("Light budget breached")
    return s


admission("unseen-before-first-sample")
job = ns["OwnedJob"](CAP - 33554432)
try:
    marker = OUTPUT / "grandchild.json"
    gate = OUTPUT / "go"
    code = 'import subprocess,sys,pathlib,time,json; o=pathlib.Path(sys.argv[1]);\nwhile not (o/"go").exists(): time.sleep(.01)\np=subprocess.Popen([sys.executable,"-B","-c","import time; a=bytearray(1048576);time.sleep(15)"]); (o/"grandchild.json").write_text(json.dumps({"pid":p.pid}))'
    with (OUTPUT / "child.log").open("wb") as log:
        child = job.launch([sys.executable, "-B", "-c", code, str(OUTPUT)], OUTPUT, env, log)
        gate.write_text("go")
        child.wait(timeout=5)
        assert marker.exists()
        orphan = json.loads(marker.read_text())["pid"]
        s = sample(job, "first-sample-after-intermediate-exit")
        assert orphan in s["processes"], (
            "FINAL-RESOURCE-001: unseen orphan not accounted"
        )
        assert s["kernel_total_processes"] == 2 and s["kernel_active_processes"] == 1, s
        cleanup = job.cleanup()
        cleanups.append(cleanup)
        assert (
            cleanup["kernel_active_processes"] == 0 and cleanup["all_handles_signaled"]
        )
        results.append(
            {
                "case": "unseen-grandchild-before-first-sample-intermediate-exit",
                "passed": True,
                "parent_pid": child.pid,
                "grandchild_pid": orphan,
                "sample": s,
                "cleanup": cleanup,
            }
        )
finally:
    try:
        cleanups.append(job.cleanup())
    finally:
        job.close()
    (OUTPUT / "resource-samples.json").write_text(json.dumps(samples, indent=2))
    (OUTPUT / "results.json").write_text(json.dumps(results, indent=2))
    (OUTPUT / "cleanup.json").write_text(json.dumps(cleanups, indent=2))


# Further regression cases below do not invent additional historical RED runs.
def save():
    (OUTPUT / "resource-samples.json").write_text(json.dumps(samples, indent=2))
    (OUTPUT / "results.json").write_text(json.dumps(results, indent=2))
    (OUTPUT / "cleanup.json").write_text(json.dumps(cleanups, indent=2))


def run_case(label, code, check):
    admission(label)
    job = ns["OwnedJob"](CAP - 33554432)
    try:
        with (OUTPUT / (label + ".log")).open("wb") as log:
            child = job.launch([sys.executable, "-B", "-c", code, str(OUTPUT)], OUTPUT, env, log)
            check(job, child)
    finally:
        try:
            cleanups.append(job.cleanup())
        finally:
            job.close()
            save()
    results.append(dict(case=label, passed=True))
    save()


def normal(job, child):
    deadline = time.monotonic() + 5
    while True:
        s = sample(job, "normal-live-parent-grandchild")
        if s["kernel_total_processes"] == 2 and len(s["processes"]) == 2:
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert os.getpid() not in s["processes"]


run_case(
    "normal-children",
    'import subprocess,sys,time; subprocess.Popen([sys.executable,"-B","-c","import time;time.sleep(10)"]);time.sleep(10)',
    normal,
)


def timeout(job, child):
    start = time.monotonic()
    while time.monotonic() - start < 0.1:
        sample(job, "timeout-live")
        time.sleep(0.01)
    # Deterministic elapsed-time seam; unchanged production 180s boundary.
    assert ns["budget_violation"](1, 671088640, 181) == "phase timeout"
    assert child.poll() is None


run_case("timeout-path", "import time;time.sleep(10)", timeout)

# Physical allocation rejection uses a separate tiny job, NOT a lowered policy gate.
admission("native-aggregate-commit-rejection")
job = ns["OwnedJob"](33554432)
try:
    with (OUTPUT / "native-allocation.log").open("wb") as log:
        code = 'import pathlib,sys,time\ntry:\n a=bytearray(67108864)\n pathlib.Path(sys.argv[1],"allocation-unexpected").write_text("BAD")\nexcept MemoryError:\n pathlib.Path(sys.argv[1],"allocation-denied").write_text("MemoryError")\ntime.sleep(10)'
        child = job.launch([sys.executable, "-B", "-c", code, str(OUTPUT)], OUTPUT, env, log)
        deadline = time.monotonic() + 5
        while not (OUTPUT / "allocation-denied").exists():
            sample(job, "allocation-denial-wait")
            assert time.monotonic() < deadline
            assert child.poll() is None
            time.sleep(0.01)
        assert not (OUTPUT / "allocation-unexpected").exists()
        s = sample(job, "allocation-denied")
        # PeakJobMemoryUsed on this OS includes the denied request charge. The
        # rejection + saved current process commit is evidence, not peak <= limit.
        assert s["kernel_job_commit_cap"] == 33554432
        assert all(v["private_commit"] < 33554432 for v in s["processes"].values())
        results.append(
            dict(case="physical-oversized-commit-denied", passed=True, kernel=s)
        )
finally:
    try:
        cleanups.append(job.cleanup())
    finally:
        job.close()
        save()

# Synthetic fresh-free refusal. The real host is not made low-memory.
original_available = ns["available"]
try:
    ns["available"] = lambda: ADMISSION - 1
    refused = False
    try:
        admission("simulated-admission-denied-no-launch")
    except RuntimeError as exc:
        refused = "NOT ADMITTED" in str(exc)
    assert refused
    results.append(
        dict(case="simulated-admission-denied-no-launch", passed=True, simulated=True)
    )
finally:
    ns["available"] = original_available
    save()

# Simulated stale/recycled list entry points at a REAL outside process (this
# monitor). Native SAME-HANDLE membership must reject it; no actual OS PID reuse.
admission("simulated-recycled-pid-membership-rejection")
job = ns["OwnedJob"](CAP - 33554432)
try:
    original_query = job.query

    class Stale:
        assigned = 1
        count = 1
        pids = [os.getpid()]

    job.query = lambda kind, typ: Stale() if kind == 3 else original_query(kind, typ)
    s = sample(job, "simulated-recycled-pid")
    assert s["processes"] == {} and job.identities == {}
    cleanups.append(job.cleanup())
    assert ns["wink"].WaitForSingleObject(ns["wink"].GetCurrentProcess(), 0) == 258
    results.append(
        dict(
            case="simulated-recycled-pid-native-same-handle-membership",
            passed=True,
            simulated=True,
            actual_os_reuse=False,
        )
    )
finally:
    job.close()
    save()

# Invalid command proves atomic launch fails closed: no child exists to execute.
admission("launch-failure")
job = ns["OwnedJob"](CAP - 33554432)
try:
    with (OUTPUT / "invalid-command.log").open("wb") as log:
        failed = False
        try:
            job.launch([str(OUTPUT / "does-not-exist.exe")], OUTPUT, env, log)
        except OSError:
            failed = True
        assert failed and job.query(1, ns["WinAccounting"]).total == 0
    cleanups.append(job.cleanup())
    results.append(dict(case="failed-atomic-launch-no-process", passed=True))
finally:
    job.close()
    save()

# Simulated unsupported native attribute API: launch fails before CreateProcess.
admission("simulated-job-attribute-unavailable")
job = ns["OwnedJob"](CAP - 33554432)
original_update = ns["wink"].UpdateProcThreadAttribute
try:

    def unavailable(*args):
        winct.set_last_error(50)
        return 0

    ns["wink"].UpdateProcThreadAttribute = unavailable
    with (OUTPUT / "unavailable-attribute.log").open("wb") as log:
        failed = False
        try:
            job.launch(
                [sys.executable, "-B", "-c", "raise SystemExit(99)"], OUTPUT, env, log
            )
        except OSError as exc:
            failed = exc.winerror == 50
    assert failed and job.query(1, ns["WinAccounting"]).total == 0
    results.append(
        dict(
            case="simulated-unsupported-job-attribute-fail-closed",
            passed=True,
            simulated=True,
        )
    )
finally:
    ns["wink"].UpdateProcThreadAttribute = original_update
    try:
        cleanups.append(job.cleanup())
    finally:
        job.close()
        save()


# Descendants cannot request breakaway from this exclusive job.
def breakaway(job, child):
    deadline = time.monotonic() + 5
    while not (OUTPUT / "breakaway-denied").exists():
        sample(job, "breakaway-denial-wait")
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert not (OUTPUT / "breakaway-unexpected").exists()
    assert job.query(1, ns["WinAccounting"]).total == 1


code = 'import subprocess,sys,pathlib,time\ntry:\n subprocess.Popen([sys.executable,"-B","-c","raise SystemExit(0)"],creationflags=0x1000000)\n pathlib.Path(sys.argv[1],"breakaway-unexpected").write_text("BAD")\nexcept OSError:\n pathlib.Path(sys.argv[1],"breakaway-denied").write_text("denied")\ntime.sleep(10)'
run_case("physical-breakaway-denied", code, breakaway)

# Real abrupt monitor exit: inner job's noninherited final handle closes while
# its child/grandchild are alive. Outer job is only a safety net, not the kill.
controller = OUTPUT / "crash-controller.py"
controller.write_text("""import os,sys,pathlib,subprocess,time,json
source=pathlib.Path(sys.argv[1]); out=pathlib.Path(sys.argv[2]); ns=dict(os=os,subprocess=subprocess,time=time,OWNED_CAP=2147483648,HOST_RESERVE=536870912,MARGIN=134217728)
t=source.read_text(); exec(compile(t[t.index('# Native authority'):t.index('sources =')],str(source),'exec'),ns)
j=ns['OwnedJob'](100663296)
with (out/'inner-child.log').open('wb') as log:
 p=j.launch([sys.executable,'-B','-c','import subprocess,sys,time; subprocess.Popen([sys.executable,"-B","-c","import time;time.sleep(10)"]);time.sleep(10)'],out,os.environ.copy(),log)
 (out/'controller-ready').write_text(str(p.pid))
 deadline=time.monotonic()+5
 while not (out/'crash-go').exists():
  if time.monotonic()>deadline: raise TimeoutError('controller gate')
  time.sleep(.01)
 os._exit(23)
""")


def crashclose(job, child):
    deadline = time.monotonic() + 5
    while True:
        s = sample(job, "before-abrupt-controller-exit")
        if len(s["processes"]) == 3 and (OUTPUT / "controller-ready").exists():
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    (OUTPUT / "crash-go").write_text("go")
    assert child.wait(timeout=5) == 23
    deadline = time.monotonic() + 5
    while job.query(1, ns["WinAccounting"]).active:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    # Check exact retained handles BEFORE outer safety-net cleanup.
    readback = []
    for (pid, born), handle in job.identities.items():
        assert ns["wink"].WaitForSingleObject(handle, 5000) == 0
        readback.append(dict(pid=pid, created_filetime=born, handle_signaled=True))
    (OUTPUT / "abrupt-exit-readback.json").write_text(
        json.dumps(
            dict(kernel_active_before_outer_cleanup=0, identities=readback), indent=2
        )
    )


admission("kill-on-close-controller-exit")
job = ns["OwnedJob"](CAP - 33554432)
try:
    with (OUTPUT / "crash-controller.log").open("wb") as log:
        child = job.launch(
            [sys.executable, "-B", str(controller), str(SOURCE), str(OUTPUT)], OUTPUT, env, log
        )
        crashclose(job, child)
    results.append(
        dict(case="physical-kill-on-close-abrupt-controller-exit", passed=True)
    )
finally:
    try:
        cleanups.append(job.cleanup())
    finally:
        job.close()
        save()

assert ns["budget_violation"](2147483648, 671088640, 180) is None
assert ns["budget_violation"](2147483649, 671088640, 1) == "owned RSS cap exceeded"
assert ns["budget_violation"](1, 671088639, 1) == "host reserve/margin breached"
results.append(dict(case="unchanged-production-budget-boundaries", passed=True))
save()
print(json.dumps({"passed": len(results), "results": results}, indent=2))


# Additional explicitly approved stdin-only regressions; old eleven cases above remain byte-identical.
def run_stdin_contract(ns, out, env):
    """Real tiny-process stdin contract; additional to historical guard cases."""
    import json
    import pathlib
    import sys

    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=False)
    child = r"""import ctypes as c, json, os
from ctypes import wintypes as w
k=c.WinDLL('kernel32', use_last_error=True)
k.GetStdHandle.argtypes=[w.DWORD]; k.GetStdHandle.restype=w.HANDLE
k.GetFileType.argtypes=[w.HANDLE]; k.GetFileType.restype=w.DWORD
handles=[k.GetStdHandle(n & 0xffffffff) for n in (-10,-11,-12)]
r={'handles':handles,'types':[k.GetFileType(h) for h in handles]}
try: r['fd0_read_hex']=os.read(0,1).hex()
except OSError as e: r['fd0_error']={'errno':e.errno,'text':str(e)}
print(json.dumps(r),flush=True)
"""
    job = ns["OwnedJob"](67108864)
    result = {"case": "stdin-valid-devnull-eof-real-child", "passed": False}
    try:
        with (out / "stdin-child.log").open("wb") as log:
            p = job.launch([sys.executable, "-B", "-c", child], out, env, log)
            result["child_exit"] = p.wait(5)
            result["sample"] = job.capture()
        result["child_observed"] = json.loads((out / "stdin-child.log").read_text())
        r = result["child_observed"]
        result["passed"] = (
            result["child_exit"] == 0
            and all(r["types"])
            and r.get("fd0_read_hex") == ""
            and "fd0_error" not in r
        )
    finally:
        try:
            result["cleanup"] = job.cleanup()
        finally:
            job.close()
        (out / "results.json").write_text(json.dumps([result], indent=2))
    assert result["passed"], (
        "C2-STDIN-CONTRACT-001: child requires valid public devnull stdin and EOF",
        result,
    )
    return [result]


def run_stdin_regressions(ns, out, env):
    """Real inheritance plus injected boundary failures with native readback.

    Failure injection is a deterministic boundary probe, not an OS-failure claim.
    Existing native Job assertions and historical case expectations stay separate.
    """
    import ctypes as ct
    import json
    import msvcrt
    import os
    import pathlib
    import sys
    from ctypes import wintypes as wt

    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=False)
    k = ns["wink"]
    ns["win_api"]("GetHandleInformation", [wt.HANDLE, ct.POINTER(wt.DWORD)])
    ns["win_api"]("GetProcessHandleCount", [wt.HANDLE, ct.POINTER(wt.DWORD)])

    def flags(h):
        f = wt.DWORD()
        ns["win_check"](k.GetHandleInformation(h, ct.byref(f)))
        return f.value

    def count():
        n = wt.DWORD()
        ns["win_check"](k.GetProcessHandleCount(k.GetCurrentProcess(), ct.byref(n)))
        return n.value

    child = r"""import ctypes as c, json, os, sys
from ctypes import wintypes as w
k=c.WinDLL('kernel32',use_last_error=True)
k.GetFinalPathNameByHandleW.argtypes=[w.HANDLE,w.LPWSTR,w.DWORD,w.DWORD]; k.GetFinalPathNameByHandleW.restype=w.DWORD
k.GetProcessId.argtypes=[w.HANDLE]; k.GetProcessId.restype=w.DWORD
k.QueryInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.c_void_p]; k.QueryInformationJobObject.restype=w.BOOL
h=list(map(int,sys.argv[1:5])); b=c.create_unicode_buffer(32768)
r={'fd0_hex':os.read(0,1).hex(),'excluded_file_path':''}
if k.GetFinalPathNameByHandleW(h[0],b,len(b),0): r['excluded_file_path']=b.value
r['unlisted_job_query_succeeded']=bool(k.QueryInformationJobObject(h[1],1,c.create_string_buffer(256),256,None))
r['unlisted_process_id']=k.GetProcessId(h[2]); b.value=''
r['caller_log_path']=b.value if k.GetFinalPathNameByHandleW(h[3],b,len(b),0) else ''
# Read value after the call, not the pre-call expression evaluation.
if k.GetFinalPathNameByHandleW(h[3],b,len(b),0): r['caller_log_path']=b.value
print(json.dumps(r),flush=True); os.write(2,b'stderr-public-marker\n')
"""
    results = []
    modes = (
        "success",
        "init-failure-simulated",
        "invalid-caller-log",
        "duplicate-1-failure-simulated",
        "duplicate-2-failure-simulated",
        "job-attribute-failure-simulated",
        "handle-attribute-failure-simulated",
        "nonexistent-executable-real",
        "identity-failure-after-create-simulated",
    )
    for mode in modes:
        for inherited in (0, 1):
            label = mode + "-caller-inherit-" + str(inherited)
            row = {
                "case": label,
                "passed": False,
                "failure_injection": "simulated" in mode,
            }
            originals = {
                n: getattr(k, n)
                for n in (
                    "DuplicateHandle",
                    "CloseHandle",
                    "InitializeProcThreadAttributeList",
                    "UpdateProcThreadAttribute",
                    "DeleteProcThreadAttributeList",
                    "CreateProcessW",
                )
            }
            original_identity = ns["identity_on_handle"]
            duplicate_handles = []
            closed = []
            attributes = []
            initialized = []
            deleted = []
            retained = []
            process_record = []
            job = ns["OwnedJob"](67108864)
            with (
                (out / (label + ".log")).open("w+b") as log,
                (out / (label + "-public-sentinel.txt")).open("w+b") as sentinel,
            ):
                log_h = msvcrt.get_osfhandle(log.fileno())
                sentinel_h = msvcrt.get_osfhandle(sentinel.fileno())
                ns["win_check"](k.SetHandleInformation(log_h, 1, inherited))
                ns["win_check"](k.SetHandleInformation(sentinel_h, 1, 1))
                process_h = wt.HANDLE()
                current = k.GetCurrentProcess()
                ns["win_check"](
                    originals["DuplicateHandle"](
                        current, current, current, ct.byref(process_h), 0, True, 2
                    )
                )
                baseline_flags = flags(log_h)
                baseline_count = count()

                def duplicate(*args):
                    if (
                        mode
                        == "duplicate-"
                        + str(len(duplicate_handles) + 1)
                        + "-failure-simulated"
                    ):
                        ct.set_last_error(8)
                        return 0
                    ok = originals["DuplicateHandle"](*args)
                    if ok:
                        duplicate_handles.append(
                            ct.cast(args[3], ct.POINTER(wt.HANDLE)).contents.value
                        )
                    return ok

                def close(h):
                    closed.append(h.value if hasattr(h, "value") else h)
                    return originals["CloseHandle"](h)

                def initialize(*args):
                    if args[0] is not None and mode == "init-failure-simulated":
                        ct.set_last_error(8)
                        return 0
                    ok = originals["InitializeProcThreadAttributeList"](*args)
                    if ok:
                        initialized.append(True)
                    return ok

                def delete(*args):
                    deleted.append(True)
                    return originals["DeleteProcThreadAttributeList"](*args)

                def update(*args):
                    kind = args[2]
                    if (
                        mode == "job-attribute-failure-simulated" and kind == 0x2000D
                    ) or (
                        mode == "handle-attribute-failure-simulated" and kind == 0x20002
                    ):
                        ct.set_last_error(50)
                        return 0
                    if kind == 0x20002:
                        assert args[4] == 2 * ct.sizeof(wt.HANDLE)
                        hs = list(ct.cast(args[3], ct.POINTER(wt.HANDLE * 2)).contents)
                        attributes.extend(hs)
                        assert hs == duplicate_handles and len(set(hs)) == 2
                        assert all(flags(h) & 1 for h in hs)
                        assert not set(hs) & {
                            job.handle,
                            log_h,
                            sentinel_h,
                            process_h.value,
                        }
                    return originals["UpdateProcThreadAttribute"](*args)

                def create(*args):
                    si = ct.cast(args[8], ct.POINTER(ns["WinStartupEx"])).contents
                    assert args[4] and args[5] & 0x80000 and si.startup.flags & 0x100
                    assert [
                        si.startup.stdin,
                        si.startup.stdout,
                    ] == attributes and si.startup.stderr == si.startup.stdout
                    assert flags(log_h) == baseline_flags and all(
                        flags(h) & 1 for h in attributes
                    )
                    assert flags(job.handle) & 1 == 0
                    ok = originals["CreateProcessW"](*args)
                    if ok:
                        pi = ct.cast(args[9], ct.POINTER(ns["WinProcessInfo"])).contents
                        h = wt.HANDLE()
                        ns["win_check"](
                            originals["DuplicateHandle"](
                                current, pi.process, current, ct.byref(h), 0, False, 2
                            )
                        )
                        retained.append(h.value)
                        process_record.append(
                            {
                                "pid": pi.pid,
                                "created_filetime": original_identity(h.value),
                                "handle": h.value,
                            }
                        )
                    return ok

                try:
                    for name, fn in (
                        ("DuplicateHandle", duplicate),
                        ("CloseHandle", close),
                        ("InitializeProcThreadAttributeList", initialize),
                        ("DeleteProcThreadAttributeList", delete),
                        ("UpdateProcThreadAttribute", update),
                        ("CreateProcessW", create),
                    ):
                        setattr(k, name, fn)
                    if mode == "identity-failure-after-create-simulated":

                        def identity_failure(h):
                            raise RuntimeError(
                                "public injected post-create identity failure"
                            )

                        ns["identity_on_handle"] = identity_failure
                    command = [
                        sys.executable,
                        "-B",
                        "-c",
                        child,
                        str(sentinel_h),
                        str(job.handle),
                        str(process_h.value),
                        str(log_h),
                    ]
                    if mode == "nonexistent-executable-real":
                        command = [str(out / "does-not-exist.exe")]

                    class InvalidLog:
                        def fileno(self):
                            raise ValueError("public invalid caller log")

                    caught = None
                    p = None
                    try:
                        p = job.launch(
                            command,
                            out,
                            env,
                            InvalidLog() if mode == "invalid-caller-log" else log,
                        )
                    except (OSError, ValueError, RuntimeError) as exc:
                        caught = {
                            "type": type(exc).__name__,
                            "text": str(exc),
                            "winerror": getattr(exc, "winerror", None),
                        }
                    finally:
                        for name, fn in originals.items():
                            setattr(k, name, fn)
                        ns["identity_on_handle"] = original_identity
                    assert flags(log_h) == baseline_flags and not log.closed
                    assert all(closed.count(h) == 1 for h in duplicate_handles), (
                        label,
                        duplicate_handles,
                        closed,
                    )
                    for h in duplicate_handles:
                        probe = wt.DWORD()
                        ct.set_last_error(0)
                        assert (
                            not k.GetHandleInformation(h, ct.byref(probe))
                            and ct.get_last_error() == 6
                        ), (label, h)
                    assert len(initialized) == len(deleted), (
                        label,
                        initialized,
                        deleted,
                    )
                    assert count() == baseline_count + len(retained) + (
                        1 if p else 0
                    ), (label, count(), baseline_count)
                    if mode == "success":
                        assert caught is None and p.wait(5) == 0
                        log.flush()
                        log.seek(0)
                        lines = log.read().decode().splitlines()
                        r = json.loads(lines[0])
                        assert (
                            r["fd0_hex"] == "" and not r["unlisted_job_query_succeeded"]
                        )
                        assert (
                            r["unlisted_process_id"] != os.getpid()
                            and not r["excluded_file_path"]
                            and not r["caller_log_path"]
                        ), r
                        assert lines[1] == "stderr-public-marker", lines
                        row["child_observed"] = r
                    else:
                        assert caught is not None and p is None, (label, caught)
                        if mode == "nonexistent-executable-real":
                            assert caught["winerror"] == 2, caught
                        elif mode == "invalid-caller-log":
                            assert caught["type"] == "ValueError", caught
                        elif mode == "identity-failure-after-create-simulated":
                            assert "post-create identity failure" in caught["text"], (
                                caught
                            )
                        elif "attribute" in mode:
                            assert caught["winerror"] == 50, caught
                        else:
                            assert caught["winerror"] == 8, caught
                        assert job.query(1, ns["WinAccounting"]).total == (
                            1
                            if mode == "identity-failure-after-create-simulated"
                            else 0
                        )
                    row.update(
                        caller_flags_before=baseline_flags,
                        caller_flags_after=flags(log_h),
                        caller_log_open=True,
                        owned_duplicate_handles=duplicate_handles,
                        owned_duplicates_closed_once=True,
                        initialized_attributes=len(initialized),
                        deleted_attributes=len(deleted),
                        explicit_handle_list=attributes,
                        expected_exception=caught,
                        created_processes=process_record,
                    )
                    row["cleanup"] = job.cleanup()
                    for h in retained:
                        assert k.WaitForSingleObject(h, 5000) == 0
                    row["retained_verifier_handles_signaled"] = True
                    row["passed"] = True
                finally:
                    for name, fn in originals.items():
                        setattr(k, name, fn)
                    ns["identity_on_handle"] = original_identity
                    try:
                        row["final_cleanup"] = job.cleanup()
                        for h in retained:
                            originals["CloseHandle"](h)
                    finally:
                        job.close()
                        originals["CloseHandle"](process_h)
                    # Sentinel and caller log remain caller-owned and usable here.
                    assert not log.closed and not sentinel.closed
                    log.write(b"caller-still-open\n")
                    log.flush()
                    results.append(row)
                    (out / "results.json").write_text(json.dumps(results, indent=2))
            assert row["passed"], row
    return results


additional = run_stdin_contract(ns, OUTPUT / "stdin-contract", env)
additional += run_stdin_regressions(ns, OUTPUT / "stdin-boundaries", env)
(OUTPUT / "stdin-additional-results.json").write_text(json.dumps(additional, indent=2))

suite = ET.Element(
    "testsuite",
    name="additional-stdin-contract",
    tests=str(len(additional)),
    failures=str(sum(not r["passed"] for r in additional)),
    errors="0",
    skipped="0",
)
for row in additional:
    case = ET.SubElement(suite, "testcase", name=row["case"])
    if not row["passed"]:
        ET.SubElement(
            case, "failure", message="stdin regression failed"
        ).text = json.dumps(row)
ET.ElementTree(suite).write(
    OUTPUT / "stdin-additional-junit.xml", encoding="utf-8", xml_declaration=True
)
print(
    json.dumps(
        {
            "additional_stdin_passed": len(additional),
            "historical_guard_passed": len(results),
        },
        indent=2,
    )
)
