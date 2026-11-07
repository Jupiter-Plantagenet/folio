"""Application caller composition. The consumed profile remains proof-only.

No ambient merge, process creation or output creation while building a plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import launch_profile as public

ENTRYPOINTS = (
    ("node_modules/typescript/bin/tsc", "--noEmit"),
    ("node_modules/vite/bin/vite.js", "build"),
    ("node_modules/@playwright/test/cli.js", "test", "--config", "export-ui.config.ts"),
)


@dataclass(frozen=True)
class ApplicationPolicy:
    whole: int = 2147483648
    native_job_commit: int = 2013265920
    outside_monitor: int = 134217728
    available_minimum: int = 2818572288
    reserve: int = 536870912
    margin: int = 134217728
    oldspace_mib: int = 1536
    phase_seconds: int = 180
    outer_seconds: int = 585
    cleanup_seconds: int = 10


def validate_policy(policy: ApplicationPolicy) -> None:
    if (
        type(policy) is not ApplicationPolicy
        or policy != ApplicationPolicy()
        or any(type(getattr(policy, k)) is not int for k in policy.__dataclass_fields__)
    ):
        raise ValueError("APPLICATION_RESOURCE_POLICY")


@dataclass(frozen=True)
class ApplicationPlan:
    profile: public.LaunchProfile
    evidence: Path
    environment: tuple[tuple[str, str], ...]
    commands: tuple[tuple[str, ...], ...]
    policy: ApplicationPolicy = ApplicationPolicy()

    def node_command(self, relative: str, *arguments: str) -> tuple[str, ...]:
        validate_application_plan(self)
        path = Path(relative)
        if (
            path.is_absolute()
            or path.drive
            or any(p in (".", "..") for p in path.parts)
        ):
            raise ValueError("OWNED_NODE_COMMAND")
        target = self.profile.layout.cwd / path
        public._native_absolute(target)
        public._no_reparse(target)
        if not target.is_file() or any(
            type(a) is not str or any(c in a for c in "\x00\r\n") for a in arguments
        ):
            raise ValueError("OWNED_NODE_COMMAND")
        return (str(self.profile.pins.node.path), str(target), *arguments)


def build_application_plan(
    profile: public.LaunchProfile, evidence: Path
) -> ApplicationPlan:
    public.validate_profile(profile)
    public._native_absolute(evidence)
    if evidence.parent != profile.layout.root or not re.fullmatch(
        r"evidence(?:-[a-z0-9-]+)?", evidence.name
    ):
        raise ValueError("OWNED_EVIDENCE_NAMESPACE")
    public._no_reparse(evidence.parent)
    if evidence.exists() or evidence.is_symlink():
        raise ValueError("FRESH_EVIDENCE_REQUIRED")
    environment = (*profile.environment, ("FOLIO_EXPORT_UI_EVIDENCE", str(evidence)))
    commands = tuple((str(profile.pins.node.path), *tail) for tail in ENTRYPOINTS)
    plan = ApplicationPlan(profile, evidence, environment, commands)
    validate_application_plan(plan)
    return plan


def validate_application_plan(plan: ApplicationPlan) -> None:
    if type(plan) is not ApplicationPlan:
        raise ValueError("IMMUTABLE_APPLICATION_PLAN")
    public.validate_profile(plan.profile)
    validate_policy(plan.policy)
    public._native_absolute(plan.evidence)
    public._no_reparse(plan.evidence.parent)
    if plan.evidence.parent != plan.profile.layout.root or not re.fullmatch(
        r"evidence(?:-[a-z0-9-]+)?", plan.evidence.name
    ):
        raise ValueError("OWNED_EVIDENCE_NAMESPACE")
    import os

    if os.path.lexists(plan.evidence):
        raise ValueError("FRESH_EVIDENCE_REQUIRED")
    if type(plan.environment) is not tuple:
        raise ValueError("IMMUTABLE_APPLICATION_ENVIRONMENT")
    extension = tuple(
        p
        for p in plan.environment
        if type(p) is tuple and len(p) == 2 and p[0] == "FOLIO_EXPORT_UI_EVIDENCE"
    )
    if extension != (("FOLIO_EXPORT_UI_EVIDENCE", str(plan.evidence)),):
        raise ValueError("APPLICATION_EVIDENCE_EXTENSION")
    baseline = tuple(
        p
        for p in plan.environment
        if not (type(p) is tuple and len(p) == 2 and p[0] == "FOLIO_EXPORT_UI_EVIDENCE")
    )
    public.validate_environment(
        baseline, plan.profile.layout, plan.profile.pins, plan.profile.sink_port
    )
    if plan.environment != (*plan.profile.environment, *extension):
        raise ValueError("APPLICATION_ENVIRONMENT_ORDER_OR_DUPLICATE")
    if type(plan.commands) is not tuple or plan.commands != tuple(
        (str(plan.profile.pins.node.path), *tail) for tail in ENTRYPOINTS
    ):
        raise ValueError("EXACT_APPLICATION_COMMANDS")


def application_admission(
    policy: ApplicationPolicy,
    available: int,
    monitor_rss: int,
    monitor_commit: int,
    outside_job: bool,
) -> dict:
    """Pure decision; execution persists ALL metrics before asserting admitted."""
    validate_policy(policy)
    if (
        any(
            type(x) is not int or x < 0
            for x in (available, monitor_rss, monitor_commit)
        )
        or type(outside_job) is not bool
    ):
        raise ValueError("APPLICATION_METRIC_TYPES")
    predicates = {
        "fresh_available": available >= policy.available_minimum,
        "outside_rss": monitor_rss <= policy.outside_monitor,
        "outside_commit": monitor_commit <= policy.outside_monitor,
        "monitor_outside_job": outside_job,
    }
    return {
        "available": available,
        "required": policy.available_minimum,
        "whole": policy.whole,
        "native_job_commit": policy.native_job_commit,
        "outside_allowance": policy.outside_monitor,
        "monitor_rss": monitor_rss,
        "monitor_private_commit": monitor_commit,
        "reserve": policy.reserve,
        "margin": policy.margin,
        "predicates": predicates,
        "admitted": all(predicates.values()),
        "authority": "APPLICATION_POLICY_NOT_PUBLIC_PROFILE_RESOURCE_CONTRACT",
    }


def application_violation(
    policy: ApplicationPolicy,
    whole_rss: int,
    monitor_rss: int,
    monitor_commit: int,
    available: int,
    phase_seconds: float,
    outer_seconds: float,
) -> str | None:
    validate_policy(policy)
    if whole_rss > policy.whole:
        return "WHOLE_RSS"
    if max(monitor_rss, monitor_commit) > policy.outside_monitor:
        return "OUTSIDE_MONITOR"
    if available < policy.reserve + policy.margin:
        return "HOST_RESERVE_MARGIN"
    if phase_seconds > policy.phase_seconds:
        return "PHASE_WALL"
    if outer_seconds > policy.outer_seconds:
        return "OUTER_WALL"
    return None


# These are immutable provider bytes, not permission to modify native helpers.
NATIVE_SHA256 = "be5f6c5098681ea36a9daec8b4b20d0685d1a34f18167adab914facd39aae61a"
DACL_SHA256 = "339184b1972e66d62593524d1a3e9d1e5ce41a3f66716787a13524ed039e4795"


@dataclass(frozen=True)
class ExecutionInputs:
    native_helper: Path
    dacl_helper: Path
    files: tuple[tuple[str, str], ...]
    root_acl: str
    dependency_identity: tuple[int, int]


def _sha(path: Path) -> str:
    import hashlib

    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _load(name: str, path: Path) -> Any:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("HELPER_IMPORT")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _save(path: Path, value: object) -> None:
    import json
    import os

    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class _InputLocks:
    def __init__(self, native: Any) -> None:
        self.native = native
        self.handles = []
        native.win_api(
            "CreateFileW",
            [
                native.winw.LPCWSTR,
                native.winw.DWORD,
                native.winw.DWORD,
                native.winct.c_void_p,
                native.winw.DWORD,
                native.winw.DWORD,
                native.winw.HANDLE,
            ],
            native.winw.HANDLE,
        )

    def acquire(self, files: tuple[tuple[str, str], ...]) -> None:
        for name, digest in files:
            path = Path(name)
            public._no_reparse(path)
            handle = self.native.wink.CreateFileW(
                str(path), 0x80000000, 1, None, 3, 0x80, None
            )
            if handle == self.native.winct.c_void_p(-1).value:
                raise self.native.winct.WinError(self.native.winct.get_last_error())
            self.handles.append(handle)
            if _sha(path) != digest:
                raise ValueError("EXECUTION_INPUT_DRIFT")

    def close(self) -> None:
        for handle in self.handles:
            self.native.wink.CloseHandle(handle)
        self.handles = []


def _tree_files(root: Path, *, web: bool = False) -> set[str]:
    import os

    result = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        public._no_reparse(base)
        for name in list(dirs):
            path = base / name
            if web and base == root and name == "node_modules":
                dirs.remove(name)
                continue
            if not web and base == root and name in (".vite", ".vite-temp"):
                public._no_reparse(path)
                dirs.remove(name)
                continue
            public._no_reparse(path)
        for name in files:
            path = base / name
            public._no_reparse(path)
            result.add(str(path))
    return result


def validate_execution_inputs(plan: ApplicationPlan, inputs: ExecutionInputs) -> None:
    import sys

    if (
        type(inputs) is not ExecutionInputs
        or type(inputs.files) is not tuple
        or not inputs.files
    ):
        raise ValueError("APPLICATION_INPUT_LOCKS_REQUIRED")
    names = set()
    for pair in inputs.files:
        if type(pair) is not tuple or len(pair) != 2:
            raise ValueError("INPUT_PAIR")
        name, digest = pair
        path = Path(name)
        public._native_absolute(path)
        public._no_reparse(path)
        if (
            str(path) != name
            or name.casefold() in names
            or not re.fullmatch("[0-9a-f]{64}", digest)
        ):
            raise ValueError("INPUT_PIN")
        names.add(name.casefold())
    inventory = dict(inputs.files)
    for helper, expected in (
        (inputs.native_helper, NATIVE_SHA256),
        (inputs.dacl_helper, DACL_SHA256),
    ):
        public._native_absolute(helper)
        public._no_reparse(helper)
        if inventory.get(str(helper)) != expected or _sha(helper) != expected:
            raise ValueError("REVIEWED_NATIVE_HELPER_PIN")
    web = plan.profile.layout.cwd
    deps = plan.profile.layout.root / "deps/node_modules"
    junction = web / "node_modules"
    public._no_reparse(deps)
    if not junction.is_junction() or junction.resolve() != deps.resolve():
        raise ValueError("PRIVATE_DEPENDENCY_JUNCTION")
    st = junction.lstat()
    if inputs.dependency_identity != (st.st_dev, st.st_ino):
        raise ValueError("DEPENDENCY_JUNCTION_IDENTITY")
    required = (
        _tree_files(web, web=True)
        | _tree_files(deps)
        | _tree_files(plan.profile.pins.python.path.parent)
    )
    required.update(
        str(pin.path)
        for pin in (
            plan.profile.pins.node,
            plan.profile.pins.python,
            plan.profile.pins.cmd,
        )
    )
    required.update(
        str(Path(m.__file__).absolute())
        for m in tuple(sys.modules.values())
        if getattr(m, "__file__", None)
    )
    required.update((str(inputs.native_helper), str(inputs.dacl_helper)))
    if not required.issubset(inventory):
        raise ValueError("COMPLETE_ACTUAL_INPUT_CLOSURE")
    for tail in ENTRYPOINTS:
        actual = deps / Path(tail[0]).relative_to("node_modules")
        if str(actual) not in inventory or not actual.is_file():
            raise ValueError("APPLICATION_DEPENDENCY_INPUT")
    for directory in (plan.profile.layout.root, web):
        if any(
            (directory / name).exists()
            for name in (
                ".env",
                ".env.local",
                ".env.production",
                ".env.production.local",
                ".env.development",
                ".env.development.local",
                ".npmrc",
                ".local",
            )
        ):
            raise ValueError("NO_PRIVATE_CONFIGURATION_COPY")
    if '"' + public.WEB_SERVER_COMMAND + '"' not in (
        web / "export-ui.config.ts"
    ).read_text(encoding="utf-8"):
        raise ValueError("STRICT_WEBSERVER_COMMAND")


def execute_application(
    plan: ApplicationPlan, inputs: ExecutionInputs | None = None
) -> int:
    """Future execution path. This packet NEVER invokes these application phases."""
    import os
    import socket
    import sys
    import time

    validate_application_plan(plan)
    if inputs is None:
        raise ValueError("APPLICATION_INPUT_LOCKS_REQUIRED")
    if (
        not sys.flags.isolated
        or not sys.flags.utf8_mode
        or not sys.dont_write_bytecode
        or Path(sys.executable) != plan.profile.pins.python.path
    ):
        raise ValueError("EXACT_ISOLATED_APPLICATION_MONITOR")
    validate_execution_inputs(plan, inputs)
    native = _load("_folio_reviewed_application_native", inputs.native_helper)
    dacl = _load("_folio_reviewed_application_dacl", inputs.dacl_helper)
    acl = dacl.read(plan.profile.layout.root)
    if acl != inputs.root_acl or acl != "O:" + dacl.sid() + dacl.descriptor():
        raise ValueError("PRIVATE_APPLICATION_DACL")
    locks = _InputLocks(native)
    sink = None
    created = False
    result = {
        "status": "NOT_ADMITTED",
        "application_policy": plan.policy.__dict__,
        "profile_scope_unchanged": plan.profile.execution_scope,
        "proof_resources_unchanged": plan.profile.resources.__dict__,
        "phases": [],
        "cleanup": None,
        "monitor": {
            "pid": os.getpid(),
            "created_filetime": native.identity_on_handle(
                native.wink.GetCurrentProcess()
            ),
        },
    }
    try:
        locks.acquire(inputs.files)
        validate_execution_inputs(plan, inputs)
        sink = socket.socket()
        sink.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sink.bind(("127.0.0.1", plan.profile.sink_port))
        result["exclusive_nonlistening_sink"] = {
            "address": sink.getsockname(),
            "listening": False,
        }
        with socket.socket() as port:
            port.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            port.bind(("127.0.0.1", 15161))
        plan.evidence.mkdir(exist_ok=False)
        created = True
        _save(plan.evidence / "environment.json", dict(plan.environment))
        _save(plan.evidence / "source-pins.json", dict(inputs.files))
        outer = time.monotonic()
        for index, command in enumerate(plan.commands):
            job = native.OwnedJob(plan.policy.native_job_commit)
            child = None
            phase = {
                "command": command,
                "cleanup": None,
                "admission": None,
                "exit_code": None,
            }
            result["phases"].append(phase)
            try:
                with (plan.evidence / f"phase-{index}.log").open("wb") as log:
                    child = job.launch(
                        list(command),
                        plan.profile.layout.cwd,
                        dict(plan.environment),
                        log,
                    )
                    identity = {
                        "pid": child.pid,
                        "created_filetime": native.identity_on_handle(child.handle),
                        "member": job.member(child.handle),
                        "initial_thread_suspended": True,
                    }
                    _save(plan.evidence / f"phase-{index}-preexec.json", identity)
                    if any(_sha(Path(n)) != h for n, h in inputs.files):
                        raise ValueError("PREEXEC_INPUT_DRIFT")
                    own = native.memory_on_handle(native.wink.GetCurrentProcess())
                    free = native.available()
                    admission = application_admission(
                        plan.policy,
                        free,
                        own["rss"],
                        own["private_commit"],
                        not job.member(native.wink.GetCurrentProcess()),
                    )
                    admission.update(
                        identity=identity,
                        continuous_input_locks=len(locks.handles),
                        fresh_after_all_preparation=True,
                        seconds_since_outer_start=time.monotonic() - outer,
                    )
                    phase["admission"] = admission
                    _save(plan.evidence / f"phase-{index}-admission.json", admission)
                    if not identity["member"] or not admission["admitted"]:
                        raise RuntimeError("APPLICATION_RESOURCE_NOT_ADMITTED")
                    if time.monotonic() - outer > plan.policy.outer_seconds:
                        raise RuntimeError("OUTER_WALL")
                    native.resume(child)
                    started = time.monotonic()
                    with (plan.evidence / f"phase-{index}-samples.jsonl").open(
                        "w", encoding="utf-8"
                    ) as samples:
                        while True:
                            snapshot = job.capture()
                            own = native.memory_on_handle(
                                native.wink.GetCurrentProcess()
                            )
                            free = native.available()
                            row = {
                                "job": snapshot,
                                "monitor": own,
                                "available": free,
                                "whole_rss": snapshot["rss"] + own["rss"],
                                "phase_seconds": time.monotonic() - started,
                                "outer_seconds": time.monotonic() - outer,
                            }
                            import json

                            samples.write(json.dumps(row) + "\n")
                            samples.flush()
                            violation = application_violation(
                                plan.policy,
                                row["whole_rss"],
                                own["rss"],
                                own["private_commit"],
                                free,
                                row["phase_seconds"],
                                row["outer_seconds"],
                            )
                            if violation:
                                raise RuntimeError(violation)
                            if child.poll() is not None:
                                break
                            time.sleep(0.05)
                    phase["exit_code"] = child.wait(5)
                    if phase["exit_code"] != 0:
                        raise RuntimeError("APPLICATION_PHASE_NONZERO")
            finally:
                try:
                    if child:
                        native.close_startup_thread(child)
                    phase["cleanup"] = job.cleanup()
                    result["cleanup"] = phase["cleanup"]
                    if child:
                        child.wait(5)
                except BaseException as exc:
                    phase["cleanup"] = {"verified": False, "error": repr(exc)}
                    result["cleanup"] = phase["cleanup"]
                    raise
                finally:
                    try:
                        _save(
                            plan.evidence / f"phase-{index}-cleanup.json",
                            phase["cleanup"],
                        )
                    finally:
                        job.close()
        with socket.socket() as port:
            result["owned_listener_closed"] = port.connect_ex(("127.0.0.1", 15161)) != 0
        result["source_pins_unchanged"] = all(
            _sha(Path(n)) == h for n, h in inputs.files
        )
        if not result["owned_listener_closed"] or not result["source_pins_unchanged"]:
            raise RuntimeError("FINAL_APPLICATION_READBACK")
        result["status"] = "PASS_SCOPED_THREE_APPLICATION_COMMANDS_NOT_FULL_RELEASE"
    except BaseException as exc:
        result["status"] = "STOPPED_NO_RETRY"
        result["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        locks.close()
        if sink is not None:
            sink.close()
        result["sink_closed"] = True
        if created:
            _save(plan.evidence / "execution.json", result)
    if result["status"] != "PASS_SCOPED_THREE_APPLICATION_COMMANDS_NOT_FULL_RELEASE":
        raise RuntimeError(result.get("error", "APPLICATION_FAILED"))
    return 0


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_REQUEST_KEY")
        result[key] = value
    return result


def load_request(path: Path) -> tuple[public.LaunchProfile, Path, ExecutionInputs]:
    import json

    public._native_absolute(path)
    public._no_reparse(path)
    request = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs
    )
    if set(request) != {"root", "pins", "sink_port", "evidence", "execution"}:
        raise ValueError("VALIDATED_REQUEST_SCHEMA")
    pins = request["pins"]
    if set(pins) != {"node", "python", "cmd"}:
        raise ValueError("PIN_ROLES")
    runtime = public.RuntimePins(
        *(
            public.ExecutablePin(Path(pins[k]["path"]), pins[k]["sha256"])
            for k in ("node", "python", "cmd")
        )
    )
    profile = public.build_profile(
        public.OwnedLayout(Path(request["root"])), runtime, request["sink_port"]
    )
    ex = request["execution"]
    if set(ex) != {
        "native_helper",
        "dacl_helper",
        "files",
        "root_acl",
        "dependency_identity",
    }:
        raise ValueError("EXECUTION_SCHEMA")
    files = tuple(tuple(pair) for pair in ex["files"])
    # Descriptor locks itself by an externally computed digest, never a self-hash.
    if str(path) in dict(files):
        raise ValueError("REQUEST_SELF_REFERENCE")
    files = (*files, (str(path), _sha(path)))
    inputs = ExecutionInputs(
        Path(ex["native_helper"]),
        Path(ex["dacl_helper"]),
        files,
        ex["root_acl"],
        tuple(ex["dependency_identity"]),
    )
    return profile, Path(request["evidence"]), inputs
