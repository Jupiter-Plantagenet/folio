"""Loopback-only compiled Folio UI/API using the existing runtime principals.

No migrations, account setup, processing workers, or credential-file creation.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATABASE_KEYS = ("FOLIO_DSN", "FOLIO_TARGET_DSN", "FOLIO_TARGET_READ_DSN")


def runtime_environment(profile, existing, port):
    if any(
        type(profile.get(key)) is not str or not profile[key] for key in DATABASE_KEYS
    ):
        raise RuntimeError("DATABASE_PROFILE_REQUIRED")
    if any(existing.get(key) != profile[key] for key in DATABASE_KEYS):
        raise RuntimeError("EXISTING_DATABASE_PRINCIPALS_REQUIRED")
    if existing.get("FOLIO_SCHEMA_MODE") != "runtime":
        raise RuntimeError("EXISTING_RUNTIME_MODE_REQUIRED")
    public = {
        "SYSTEMROOT": "C:/Windows",
        "WINDIR": "C:/Windows",
        "PATH": "C:/Windows/System32",
        "HOME": str(ROOT),
        "USERPROFILE": str(ROOT),
    }
    public.update({key: profile[key] for key in DATABASE_KEYS})
    public.update(
        FOLIO_SCHEMA_MODE="runtime", FOLIO_UI_ORIGIN=f"http://127.0.0.1:{port}"
    )
    for key in (
        "FOLIO_EXPORT_ROOT",
        "FOLIO_TARGET_URL",
        "FOLIO_VERIFIER_IDENTITY",
        "FOLIO_APPROVER_IDENTITY",
        "FOLIO_TARGET_IDENTITY",
        "TEMP",
        "TMP",
        "TMPDIR",
    ):
        if existing.get(key):
            public[key] = existing[key]
    return public


def ui_directory(value):
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    for item in [path, *path.parents]:
        # Windows reparse points and POSIX symlinks are both refused.
        if item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 1024:
            raise RuntimeError("UI_REPARSE_REFUSED")
    path = path.resolve(strict=True)
    if not path.is_relative_to(ROOT / "web" / ".cache") or not path.name.startswith(
        "application-build-"
    ):
        raise RuntimeError("OWNED_UI_BUILD_REQUIRED")
    if not (path / "index.html").is_file():
        raise RuntimeError("UI_BUILD_REQUIRED")
    return path


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--ui-dir", required=True)
    cli.add_argument("--port", type=int, default=8111)
    cli.add_argument("--source-pid", type=int)
    cli.add_argument("--source-created", type=float)
    cli.add_argument("--serve-child", action="store_true", help=argparse.SUPPRESS)
    args = cli.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("PYTHON312_REQUIRED")
    if not 1024 <= args.port <= 65535:
        raise RuntimeError("LOOPBACK_PORT_REQUIRED")
    ui = ui_directory(args.ui_dir)
    if args.serve_child:
        if os.environ.get("FOLIO_SCHEMA_MODE") != "runtime":
            raise RuntimeError("RUNTIME_MODE_REQUIRED")
        sys.path.insert(0, str(ROOT))
        import uvicorn
        from fastapi.staticfiles import StaticFiles

        from folio.workbench import app

        app.mount("/", StaticFiles(directory=str(ui), html=True), name="compiled-ui")
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
        return
    import psutil

    if args.source_pid is None or args.source_created is None:
        raise RuntimeError("EXISTING_API_IDENTITY_REQUIRED")
    source = psutil.Process(args.source_pid)
    if (
        source.create_time() != args.source_created
        or Path(source.cwd()) != ROOT
        or not any("folio.workbench" in item for item in source.cmdline())
    ):
        raise RuntimeError("EXISTING_API_IDENTITY_MISMATCH")
    existing = source.environ()
    if not source.is_running():
        raise RuntimeError("EXISTING_API_CHANGED")
    if any(
        row.status == "LISTEN" and row.laddr and row.laddr.port == args.port
        for row in psutil.net_connections(kind="tcp")
    ):
        raise RuntimeError("LOOPBACK_PORT_IN_USE")
    profiles = json.loads(
        (ROOT / ".local/database-profiles.json").read_text(encoding="utf-8")
    )
    env = runtime_environment(profiles["browser"], existing, args.port)
    argv = [
        sys.executable,
        "-I",
        "-B",
        "-X",
        "utf8",
        str(Path(__file__).resolve()),
        "--serve-child",
        "--ui-dir",
        str(ui),
        "--port",
        str(args.port),
    ]
    child = subprocess.Popen(argv, cwd=ROOT, env=env, shell=False)
    try:
        return child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        code = str(error)
        print(
            code
            if code.replace("_", "").isupper() and code.isascii()
            else "LOCAL_RUNTIME_START_FAILED:" + type(error).__name__,
            file=sys.stderr,
        )
        raise SystemExit(1)
