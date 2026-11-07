"""Owned isolated-browser lane only. No DB/profile/worker/service activation.
Candidate: validated-profile application caller; legacy positional output is refused.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

WEB = Path(__file__).resolve().parents[1]
LIGHT = 939524096
HEAVY = 6442450944
OWNED_CAP = 2147483648
HOST_RESERVE = 536870912
MARGIN = 134217728
ADMISSION = max(LIGHT, OWNED_CAP + HOST_RESERVE + MARGIN)

# Native authority is private to this namespace; SQL `as c` cannot clobber it.
import ctypes as winct  # noqa: E402 - runner-guard executes this native boundary separately
from ctypes import wintypes as winw  # noqa: E402


class WinMemory(winct.Structure):
    _fields_ = [("length", winw.DWORD), ("load", winw.DWORD)] + [
        (n, winct.c_ulonglong)
        for n in ("total", "available", "tp", "ap", "tv", "av", "ex")
    ]


class WinCounters(winct.Structure):
    _fields_ = [("cb", winw.DWORD), ("faults", winw.DWORD)] + [
        (n, winct.c_size_t)
        for n in (
            "peak",
            "working",
            "qp",
            "q",
            "qnp",
            "qn",
            "page",
            "peakpage",
            "private",
        )
    ]


class WinBasicLimit(winct.Structure):
    _fields_ = [
        ("process_time", winct.c_longlong),
        ("job_time", winct.c_longlong),
        ("flags", winw.DWORD),
        ("min_ws", winct.c_size_t),
        ("max_ws", winct.c_size_t),
        ("active_limit", winw.DWORD),
        ("affinity", winct.c_size_t),
        ("priority", winw.DWORD),
        ("scheduling", winw.DWORD),
    ]


class WinLimits(winct.Structure):
    _fields_ = [
        ("basic", WinBasicLimit),
        ("io", winct.c_ulonglong * 6),
        ("process_memory", winct.c_size_t),
        ("job_memory", winct.c_size_t),
        ("peak_process", winct.c_size_t),
        ("peak_job", winct.c_size_t),
    ]


class WinAccounting(winct.Structure):
    _fields_ = [
        ("times", winct.c_longlong * 4),
        ("faults", winw.DWORD),
        ("total", winw.DWORD),
        ("active", winw.DWORD),
        ("terminated", winw.DWORD),
    ]


class WinStartup(winct.Structure):
    _fields_ = [
        ("cb", winw.DWORD),
        ("reserved", winw.LPWSTR),
        ("desktop", winw.LPWSTR),
        ("title", winw.LPWSTR),
        ("x", winw.DWORD),
        ("y", winw.DWORD),
        ("xs", winw.DWORD),
        ("ys", winw.DWORD),
        ("xc", winw.DWORD),
        ("yc", winw.DWORD),
        ("fill", winw.DWORD),
        ("flags", winw.DWORD),
        ("show", winw.WORD),
        ("reserved_size", winw.WORD),
        ("reserved_ptr", winct.POINTER(winct.c_byte)),
        ("stdin", winw.HANDLE),
        ("stdout", winw.HANDLE),
        ("stderr", winw.HANDLE),
    ]


class WinStartupEx(winct.Structure):
    _fields_ = [("startup", WinStartup), ("attributes", winct.c_void_p)]


class WinProcessInfo(winct.Structure):
    _fields_ = [
        ("process", winw.HANDLE),
        ("thread", winw.HANDLE),
        ("pid", winw.DWORD),
        ("tid", winw.DWORD),
    ]


wink = winct.WinDLL("kernel32", use_last_error=True)
winps = winct.WinDLL("psapi", use_last_error=True)


def win_api(name, args, result=winw.BOOL):
    fn = getattr(wink, name)
    fn.argtypes = args
    fn.restype = result
    return fn


win_api("CloseHandle", [winw.HANDLE])
win_api("CreateJobObjectW", [winct.c_void_p, winw.LPCWSTR], winw.HANDLE)
win_api(
    "SetInformationJobObject", [winw.HANDLE, winct.c_int, winct.c_void_p, winw.DWORD]
)
win_api(
    "QueryInformationJobObject",
    [winw.HANDLE, winct.c_int, winct.c_void_p, winw.DWORD, winct.c_void_p],
)
win_api("IsProcessInJob", [winw.HANDLE, winw.HANDLE, winct.POINTER(winw.BOOL)])
win_api("GetCurrentProcess", [], winw.HANDLE)
win_api("TerminateJobObject", [winw.HANDLE, winw.UINT])
win_api("OpenProcess", [winw.DWORD, winw.BOOL, winw.DWORD], winw.HANDLE)
win_api("GetProcessTimes", [winw.HANDLE] + [winct.POINTER(winw.FILETIME)] * 4)
win_api("WaitForSingleObject", [winw.HANDLE, winw.DWORD], winw.DWORD)
win_api("GetExitCodeProcess", [winw.HANDLE, winct.POINTER(winw.DWORD)])
win_api(
    "InitializeProcThreadAttributeList",
    [winct.c_void_p, winw.DWORD, winw.DWORD, winct.POINTER(winct.c_size_t)],
)
win_api(
    "UpdateProcThreadAttribute",
    [
        winct.c_void_p,
        winw.DWORD,
        winct.c_size_t,
        winct.c_void_p,
        winct.c_size_t,
        winct.c_void_p,
        winct.c_void_p,
    ],
)
win_api("DeleteProcThreadAttributeList", [winct.c_void_p], None)
win_api(
    "CreateProcessW",
    [
        winw.LPCWSTR,
        winw.LPWSTR,
        winct.c_void_p,
        winct.c_void_p,
        winw.BOOL,
        winw.DWORD,
        winct.c_void_p,
        winw.LPCWSTR,
        winct.POINTER(WinStartupEx),
        winct.POINTER(WinProcessInfo),
    ],
)
win_api("SetHandleInformation", [winw.HANDLE, winw.DWORD, winw.DWORD])
win_api(
    "DuplicateHandle",
    [
        winw.HANDLE,
        winw.HANDLE,
        winw.HANDLE,
        winct.POINTER(winw.HANDLE),
        winw.DWORD,
        winw.BOOL,
        winw.DWORD,
    ],
)
win_api("GlobalMemoryStatusEx", [winct.POINTER(WinMemory)])
winps.GetProcessMemoryInfo.argtypes = [
    winw.HANDLE,
    winct.POINTER(WinCounters),
    winw.DWORD,
]
winps.GetProcessMemoryInfo.restype = winw.BOOL


def win_check(ok):
    if not ok:
        raise winct.WinError(winct.get_last_error())


def available():
    m = WinMemory()
    m.length = winct.sizeof(m)
    win_check(wink.GlobalMemoryStatusEx(winct.byref(m)))
    return m.available


def identity_on_handle(handle):
    a, b, d, e = (winw.FILETIME() for _ in range(4))
    win_check(
        wink.GetProcessTimes(
            handle, winct.byref(a), winct.byref(b), winct.byref(d), winct.byref(e)
        )
    )
    return (a.dwHighDateTime << 32) | a.dwLowDateTime


def memory_on_handle(handle):
    m = WinCounters()
    m.cb = winct.sizeof(m)
    win_check(winps.GetProcessMemoryInfo(handle, winct.byref(m), winct.sizeof(m)))
    return {
        "created_filetime": identity_on_handle(handle),
        "rss": m.working,
        "private_commit": m.private,
    }


def sample_process(pid):
    h = wink.OpenProcess(0x100410, False, pid)
    if not h:
        if winct.get_last_error() in (87, 1168):
            return None
        raise winct.WinError(winct.get_last_error())
    try:
        if wink.WaitForSingleObject(h, 0) == 0:
            return None
        m = memory_on_handle(h)
        return m["created_filetime"], m["rss"]
    finally:
        wink.CloseHandle(h)


class OwnedProcess:
    def __init__(self, handle, pid):
        self.handle = handle
        self.pid = pid
        self.returncode = None

    def poll(self):
        status = wink.WaitForSingleObject(self.handle, 0)
        if status == 258:
            return None
        if status != 0:
            raise RuntimeError("Process wait failed")
        code = winw.DWORD()
        win_check(wink.GetExitCodeProcess(self.handle, winct.byref(code)))
        self.returncode = code.value
        return self.returncode

    def wait(self, timeout=10):
        if wink.WaitForSingleObject(self.handle, int(timeout * 1000)) != 0:
            raise TimeoutError("Owned process did not exit")
        return self.poll()


class OwnedJob:
    """Unnamed, non-inheritable job; atomic creation membership, no breakaway.

    Monitor stays OUTSIDE this job. All descendants inherit membership even if
    their parents exit before any sample. No PPID is authority. Kernel commit
    limit is NOT an RSS limit. RSS remains an observed aggregate sampled gate.
    """

    def __init__(self, commit_cap):
        if commit_cap <= 0:
            raise ValueError("positive commit cap required")
        self.handle = wink.CreateJobObjectW(None, None)
        win_check(self.handle)
        self.identities = {}
        self.processes = []
        self.closed = False
        try:
            limits = WinLimits()
            limits.basic.flags = 0x2000 | 0x200  # kill-on-close, aggregate job commit
            limits.job_memory = commit_cap
            win_check(
                wink.SetInformationJobObject(
                    self.handle, 9, winct.byref(limits), winct.sizeof(limits)
                )
            )
            actual = self.query(9, WinLimits)
            if (
                actual.basic.flags != limits.basic.flags
                or actual.job_memory != commit_cap
            ):
                raise RuntimeError("Native job enforcement readback mismatch")
            if self.member(wink.GetCurrentProcess()):
                raise RuntimeError("Monitor must never belong to cleanup job")
            self.commit_cap = commit_cap
        except BaseException:
            wink.CloseHandle(self.handle)
            self.closed = True
            raise

    def query(self, kind, typ):
        result = typ()
        win_check(
            wink.QueryInformationJobObject(
                self.handle, kind, winct.byref(result), winct.sizeof(result), None
            )
        )
        return result

    def member(self, handle):
        answer = winw.BOOL()
        win_check(wink.IsProcessInJob(handle, self.handle, winct.byref(answer)))
        return bool(answer.value)

    def launch(self, command, cwd, env, log):
        # JOB_LIST atomically assigns BEFORE the child's initial thread executes.
        # HANDLE_LIST prevents the job/identity handles leaking into descendants.
        import msvcrt

        if self.closed:
            raise RuntimeError("Closed job")
        size = winct.c_size_t()
        wink.InitializeProcThreadAttributeList(None, 2, 0, winct.byref(size))
        buf = winct.create_string_buffer(size.value)
        win_check(wink.InitializeProcThreadAttributeList(buf, 2, 0, winct.byref(size)))
        owned_handles = []
        pi = WinProcessInfo()
        made = False
        try:
            log_handle = msvcrt.get_osfhandle(log.fileno())
            # Private inheritable duplicates leave caller-owned flags untouched.
            # Keep devnull, duplicate handles and attribute buffers alive through
            # CreateProcess; never borrow ambient standard input.
            with open(os.devnull, "rb") as null:
                current = wink.GetCurrentProcess()
                for original in (msvcrt.get_osfhandle(null.fileno()), log_handle):
                    duplicate = winw.HANDLE()
                    win_check(
                        wink.DuplicateHandle(
                            current,
                            original,
                            current,
                            winct.byref(duplicate),
                            0,
                            True,
                            2,
                        )
                    )
                    owned_handles.append(duplicate.value)
                stdin_handle, output_handle = owned_handles
                jobs = (winw.HANDLE * 1)(self.handle)
                handles = (winw.HANDLE * 2)(stdin_handle, output_handle)
                win_check(
                    wink.UpdateProcThreadAttribute(
                        buf, 0, 0x2000D, jobs, winct.sizeof(jobs), None, None
                    )
                )
                win_check(
                    wink.UpdateProcThreadAttribute(
                        buf, 0, 0x20002, handles, winct.sizeof(handles), None, None
                    )
                )
                si = WinStartupEx()
                si.startup.cb = winct.sizeof(si)
                si.attributes = winct.cast(buf, winct.c_void_p)
                si.startup.flags = 0x100
                si.startup.stdin = stdin_handle
                si.startup.stdout = output_handle
                si.startup.stderr = output_handle
                block = winct.create_unicode_buffer(
                    "\0".join(
                        k + "=" + v
                        for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())
                    )
                    + "\0\0"
                )
                line = winct.create_unicode_buffer(subprocess.list2cmdline(command))
                win_check(
                    wink.CreateProcessW(
                        None,
                        line,
                        None,
                        None,
                        True,
                        0x80000 | 0x400,
                        block,
                        str(cwd),
                        winct.byref(si),
                        winct.byref(pi),
                    )
                )
                made = True
            # Assertion only: native JOB_LIST already made assignment atomic.
            if not self.member(pi.process):
                raise RuntimeError("Created process missing native job membership")
            born = identity_on_handle(pi.process)
            self.identities[(pi.pid, born)] = pi.process
            process = OwnedProcess(pi.process, pi.pid)
            self.processes.append(process)
            return process
        except BaseException:
            if made:
                # Uses only the exclusive job, never a PID or parent relationship.
                wink.TerminateJobObject(self.handle, 1)
                wink.WaitForSingleObject(pi.process, 5000)
                wink.CloseHandle(pi.process)
            raise
        finally:
            for handle in owned_handles:
                wink.CloseHandle(handle)
            if pi.thread:
                wink.CloseHandle(pi.thread)
            wink.DeleteProcThreadAttributeList(buf)

    def capture(self):
        # A bounded oversized list, with fail-closed overflow (not silent truncation).
        class Pids(winct.Structure):
            _fields_ = [
                ("assigned", winw.DWORD),
                ("count", winw.DWORD),
                ("pids", winct.c_size_t * 4096),
            ]

        result = self.query(3, Pids)
        if result.assigned > 4096 or result.count > 4096:
            raise RuntimeError("Owned job process-list overflow")
        known = {
            pid
            for (pid, born), h in self.identities.items()
            if wink.WaitForSingleObject(h, 0) == 258
        }
        for pid in result.pids[: result.count]:
            if pid in known:
                continue
            h = wink.OpenProcess(0x100410, False, pid)
            if not h:
                if winct.get_last_error() in (87, 1168):
                    continue
                raise winct.WinError(winct.get_last_error())
            try:
                # Recycled unrelated PID fails membership on the SAME handle.
                if not self.member(h):
                    continue
                born = identity_on_handle(h)
                self.identities[(pid, born)] = h
                h = None
            finally:
                if h:
                    wink.CloseHandle(h)
        samples = {}
        for (pid, born), h in self.identities.items():
            state = wink.WaitForSingleObject(h, 0)
            if state == 0:
                continue
            if state != 258:
                raise RuntimeError("Identity wait failed")
            try:
                samples[pid] = memory_on_handle(h)
            except OSError:
                if wink.WaitForSingleObject(h, 0) == 0:
                    continue
                raise
        accounting = self.query(1, WinAccounting)
        limits = self.query(9, WinLimits)
        return {
            "processes": samples,
            "rss": sum(x["rss"] for x in samples.values()),
            "kernel_total_processes": accounting.total,
            "kernel_active_processes": accounting.active,
            "kernel_peak_job_commit": limits.peak_job,
            "kernel_job_commit_cap": limits.job_memory,
        }

    def cleanup(self):
        if self.closed:
            raise RuntimeError("Cleanup requires retained job handle")
        win_check(wink.TerminateJobObject(self.handle, 1))
        deadline = time.monotonic() + 5
        while True:
            active = self.query(1, WinAccounting).active
            if not active:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Native job cleanup readback nonzero")
            time.sleep(0.01)
        identities = []
        for (pid, born), h in self.identities.items():
            # Active count can reach zero before the process object is signaled.
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            signaled = wink.WaitForSingleObject(h, remaining_ms) == 0
            identities.append(
                {"pid": pid, "created_filetime": born, "handle_signaled": signaled}
            )
            if not signaled:
                raise RuntimeError("Owned identity survived cleanup")
        return {
            "kernel_active_processes": active,
            "identities": identities,
            "all_handles_signaled": True,
        }

    def close(self):
        if not self.closed:
            # Last handle closes kill-on-close job even when capture failed.
            wink.CloseHandle(self.handle)
            self.closed = True
            for h in self.identities.values():
                wink.CloseHandle(h)


def budget_violation(rss, free, elapsed):
    if rss > OWNED_CAP:
        return "owned RSS cap exceeded"
    if free < HOST_RESERVE + MARGIN:
        return "host reserve/margin breached"
    if elapsed > 180:
        return "phase timeout"
    return None


sources = [
    "src/main.tsx",
    "src/EvidenceExport.tsx",
    "export-ui.config.ts",
    "export-tests/export.spec.ts",
    "export-tests/run.py",
    "export-tests/runner-guard.py",
    "package-lock.json",
    "vite.config.ts",
]


def prepare_application(profile, evidence):
    from application_launch import build_application_plan

    return build_application_plan(profile, evidence)


def main(profile, evidence, *, boundary=None, execution_inputs=None):
    from application_launch import execute_application

    plan = prepare_application(profile, evidence)
    if WEB != profile.layout.cwd:
        raise ValueError("ACTUAL_CALLER_LAYOUT")
    if boundary is not None:
        boundary(plan)
    return execute_application(plan, execution_inputs)


def cli(argv=None, *, boundary=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2 or arguments[0] != "--validated-profile":
        raise ValueError("VALIDATED_PROFILE_MODE_REQUIRED")
    from application_launch import load_request

    profile, evidence, inputs = load_request(Path(arguments[1]))
    return main(profile, evidence, boundary=boundary, execution_inputs=inputs)


if __name__ == "__main__":
    raise SystemExit(cli())
