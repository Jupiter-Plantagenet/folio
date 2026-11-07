"""Mirror candidate: immutable, public-only Folio launch description.
No process creation, configuration-file reader, ambient environment merge or app API.
The Node process CALLING spawn must receive this entire validated environment.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

TRUSTED_CMD = Path("C:/Windows/System32/cmd.exe")
TRUSTED_WINDOWS = Path("C:/Windows")
OWNED_PARENT = Path("C:/Users/user/AppData/Local/hermes/cache/scratch/f84")
PYTHON_FLAGS = ("-I", "-B", "-X", "utf8")
WEB_SERVER_COMMAND = (
    "node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 15161 --strictPort"
)


@dataclass(frozen=True)
class NativeCmdPath:
    value: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, Path)
            or not self.value.is_absolute()
            or self.value != TRUSTED_CMD
        ):
            raise ValueError("CMD_IDENTITY")
        object.__setattr__(self, "value", TRUSTED_CMD)

    def materialize(self) -> str:
        return str(self.value)


@dataclass(frozen=True)
class ExecutablePin:
    path: Path
    sha256: str

    def verify(self) -> None:
        _native_absolute(self.path)
        if self.path.suffix.lower() != ".exe" or not re.fullmatch(
            r"[0-9a-f]{64}", self.sha256
        ):
            raise ValueError("EXECUTABLE_PIN_FORMAT")
        _no_reparse(self.path)
        with self.path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != self.sha256:
            raise ValueError("EXECUTABLE_PIN")


@dataclass(frozen=True)
class RuntimePins:
    node: ExecutablePin
    python: ExecutablePin
    cmd: ExecutablePin


@dataclass(frozen=True)
class OwnedLayout:
    root: Path

    @property
    def cwd(self) -> Path:
        return self.root / "web"


@dataclass(frozen=True)
class ResourceContract:
    whole: int = 536870912
    native_job_commit: int = 469762048
    outside_monitor: int = 67108864
    available_minimum: int = 1207959552
    reserve: int = 536870912
    margin: int = 134217728
    wall_seconds: int = 60
    cleanup_seconds: int = 10


@dataclass(frozen=True)
class LaunchProfile:
    layout: OwnedLayout
    pins: RuntimePins
    sink_port: int
    environment: tuple[tuple[str, str], ...]
    python_prefix: tuple[str, ...]
    resources: ResourceContract = ResourceContract()
    web_server_command: str = WEB_SERVER_COMMAND
    ui_port: int = 15161
    strict_port: bool = True
    node_oldspace_mib: int = 1536
    stdio_contract: str = "native-NUL-private-log-duplicates-explicit-HANDLE_LIST;next-Node-shell-three-pipes"
    ownership_contract: str = "preexec-JOB_LIST-initial-suspension-PID-creation-kernel-membership-zero-active-and-signaled"
    execution_scope: str = "PUBLIC_PROFILE_PROOF_ONLY_NO_APPLICATION_ADMISSION"


def _expected_environment(
    layout: OwnedLayout, pins: RuntimePins, sink_port: int
) -> tuple[tuple[str, str], ...]:
    root = layout.root
    env = {
        "SystemRoot": str(TRUSTED_WINDOWS),
        "WINDIR": str(TRUSTED_WINDOWS),
        "ComSpec": NativeCmdPath(pins.cmd.path).materialize(),
        "SystemDrive": "C:",
        "ProgramFiles": str(Path("C:/Program Files")),
        "ProgramFiles(x86)": str(Path("C:/Program Files (x86)")),
        "PATH": os.pathsep.join(
            (
                str(pins.node.path.parent),
                str(pins.python.path.parent),
                str(TRUSTED_WINDOWS / "System32"),
                str(TRUSTED_WINDOWS),
            )
        ),
        "TEMP": str(root / "tmp"),
        "TMP": str(root / "tmp"),
        "TMPDIR": str(root / "tmp"),
        "USERPROFILE": str(root / "home"),
        "HOME": str(root / "home"),
        "LOCALAPPDATA": str(root / "local"),
        "APPDATA": str(root / "roaming"),
        "NODE_OPTIONS": "--max-old-space-size=1536",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PLAYWRIGHT_BROWSERS_PATH": str(root / "browser-cache"),
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        "FOLIO_UI_PORT": "15161",
        "FOLIO_API_URL": "http://127.0.0.1:" + str(sink_port),
        "NO_COLOR": "1",
        "CI": "1",
    }
    return tuple(env.items())


def _native_absolute(path: Path) -> None:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or not re.fullmatch(r"[A-Za-z]:", path.drive)
        or any(
            part in (".", "..")
            or any(c in part for c in '\x00\r\n;&|<>"')
            or ":" in part
            for part in path.parts[1:]
        )
    ):
        raise ValueError("ABSOLUTE_NATIVE_PATH")


def _no_reparse(path: Path) -> None:
    for entry in (path, *path.parents):
        if entry.stat(follow_symlinks=False).st_file_attributes & 1024:
            raise ValueError("REPARSE_PATH")


def _validate_inputs(layout: OwnedLayout, pins: RuntimePins, sink_port: int) -> None:
    if type(layout) is not OwnedLayout or type(pins) is not RuntimePins:
        raise ValueError("STRUCTURED_INPUT_TYPES")
    _native_absolute(layout.root)
    if layout.root.parent != OWNED_PARENT or not re.fullmatch(
        r"[a-z0-9-]+", layout.root.name
    ):
        raise ValueError("OWNED_NAMESPACE")
    if (
        type(sink_port) is not int
        or not 1 <= sink_port <= 65535
        or sink_port in (15161, 8101, 8201)
    ):
        raise ValueError("POSITIVE_NON_SERVICE_SINK_PORT")
    for name in (
        "tmp",
        "home",
        "local",
        "roaming",
        "browser-cache",
        "web",
        "deps/node_modules",
    ):
        entry = layout.root / name
        if not entry.is_dir():
            raise ValueError("OWNED_LAYOUT_MISSING")
        _no_reparse(entry)
    for pin, basename in (
        (pins.node, "node.exe"),
        (pins.python, "python.exe"),
        (pins.cmd, "cmd.exe"),
    ):
        if type(pin) is not ExecutablePin or pin.path.name.lower() != basename:
            raise ValueError("EXECUTABLE_ROLE")
        _native_absolute(pin.path)
    NativeCmdPath(pins.cmd.path)


def validate_environment(
    entries: Iterable[tuple[str, str]],
    layout: OwnedLayout,
    pins: RuntimePins,
    sink_port: int,
) -> tuple[tuple[str, str], ...]:
    # Preserve duplicates until checked; never dict(), uppercase() or filter first.
    pairs = tuple(entries)
    seen: set[str] = set()
    for entry in pairs:
        if type(entry) is not tuple or len(entry) != 2:
            raise ValueError("ENVIRONMENT_PAIR")
        key, value = entry
        if (
            type(key) is not str
            or type(value) is not str
            or not key
            or "=" in key
            or any(c in key + value for c in "\x00\r\n")
        ):
            raise ValueError("ENVIRONMENT_TEXT")
        folded = key.casefold()
        if folded in seen:
            raise ValueError("DUPLICATE_ENVIRONMENT_KEY")
        seen.add(folded)
    # Validation of a materialized value is deliberately NOT normalization.
    if dict(pairs).get("ComSpec") != str(TRUSTED_CMD):
        raise ValueError("MATERIALIZED_COMSPEC")
    _validate_inputs(layout, pins, sink_port)
    expected = dict(_expected_environment(layout, pins, sink_port))
    if set(dict(pairs)) != set(expected):
        raise ValueError("PUBLIC_ENVIRONMENT_KEYSET")
    if dict(pairs) != expected:
        raise ValueError("PUBLIC_ENVIRONMENT_VALUE")
    return pairs


def validate_profile(profile: LaunchProfile) -> None:
    if type(profile) is not LaunchProfile or type(profile.environment) is not tuple:
        raise ValueError("IMMUTABLE_PROFILE")
    validate_environment(
        profile.environment, profile.layout, profile.pins, profile.sink_port
    )
    if profile.python_prefix != (str(profile.pins.python.path), *PYTHON_FLAGS):
        raise ValueError("EXPLICIT_ISOLATED_PYTHON_UTF8")
    if (
        type(profile.resources) is not ResourceContract
        or profile.resources != ResourceContract()
    ):
        raise ValueError("PROOF_RESOURCE_CONTRACT")
    for field in profile.resources.__dataclass_fields__:
        if type(getattr(profile.resources, field)) is not int:
            raise ValueError("INTEGER_RESOURCE_CONTRACT")
    fixed = {
        "web_server_command": WEB_SERVER_COMMAND,
        "ui_port": 15161,
        "strict_port": True,
        "node_oldspace_mib": 1536,
        "stdio_contract": "native-NUL-private-log-duplicates-explicit-HANDLE_LIST;next-Node-shell-three-pipes",
        "ownership_contract": "preexec-JOB_LIST-initial-suspension-PID-creation-kernel-membership-zero-active-and-signaled",
        "execution_scope": "PUBLIC_PROFILE_PROOF_ONLY_NO_APPLICATION_ADMISSION",
    }
    for name, expected in fixed.items():
        if (
            type(getattr(profile, name)) is not type(expected)
            or getattr(profile, name) != expected
        ):
            raise ValueError("FIXED_PROFILE_CONTRACT")
    for pin in (profile.pins.node, profile.pins.python, profile.pins.cmd):
        pin.verify()


def build_profile(
    layout: OwnedLayout, pins: RuntimePins, sink_port: int
) -> LaunchProfile:
    _validate_inputs(layout, pins, sink_port)
    env = _expected_environment(layout, pins, sink_port)
    profile = LaunchProfile(
        layout, pins, sink_port, env, (str(pins.python.path), *PYTHON_FLAGS)
    )
    validate_profile(profile)
    return profile
