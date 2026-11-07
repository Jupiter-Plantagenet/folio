import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
# Direct `python scripts/...py` launches put only scripts/ on sys.path. Operator
# entrypoints also import the maintained folio/ package from this checkout.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def dev_dsn(role, database):
    """Legacy dev-role DSN. Passwords never live in source: they come from
    FOLIO_DEV_DB_PASSWORD_<ROLE> or the private .local/dev-db-passwords.json; if
    neither exists the DSN has no password (libpq then uses PGPASSWORD/pgpass).
    The legacy roles folio_workbench/folio_target/folio_verifier are NOLOGIN with
    no stored password since round 6, so this path only works after an operator
    deliberately re-provisions them."""
    password = os.environ.get("FOLIO_DEV_DB_PASSWORD_" + role.upper())
    store = ROOT / ".local/dev-db-passwords.json"
    if password is None and store.exists():
        password = json.loads(store.read_text()).get(role)
    from urllib.parse import quote

    auth = role + (":" + quote(password, safe="") if password else "")
    return f"postgresql://{auth}@127.0.0.1:5541/{database}"


def is_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def environment(profile):
    env = os.environ.copy()
    if profile == "browser":
        p = json.loads(
            (ROOT / "evidence/documents/flow/latest-browser-profile.json").read_text()
        )
        env["FOLIO_DSN"] = dev_dsn("folio_workbench", p["workbench_database"])
        env["FOLIO_TARGET_DSN"] = dev_dsn("folio_target", p["target_database"])
        env["FOLIO_TARGET_READ_DSN"] = dev_dsn("folio_verifier", p["target_database"])
    private_profiles = ROOT / ".local/database-profiles.json"
    if private_profiles.exists():
        profiles = json.loads(private_profiles.read_text())
        if profile not in profiles:
            raise RuntimeError("RUNTIME_DATABASE_PROFILE_NOT_PROVISIONED")
        env.update(profiles[profile])
        env["FOLIO_SCHEMA_MODE"] = "runtime"
    return env


def stop(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def ready(port, path="/health"):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}" + path, timeout=1)
            if r.status_code == 200:
                return r.text
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Port {port} did not become ready")
