"""Fail-closed write admission and process-local active-request drain counter."""

import json
import os
import stat
import threading
from pathlib import Path

_lock = threading.Lock()
_active_mutations = 0
_active_requests = 0
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def marker_present():
    configured = os.getenv("FOLIO_MAINTENANCE_MARKER")
    if not configured:
        return False
    try:
        marker = Path(configured)
        root = Path(__file__).resolve().parents[1]
        expected_parent = root / ".local" / "maintenance"
        expected = expected_parent / "folio-quiesce.json"
        if not marker.is_absolute():
            return True
        # A single project-owned path keeps components on the same marker and
        # prevents an environment value from making the API inspect arbitrary files.
        if os.path.normcase(str(marker.resolve(strict=False))) != os.path.normcase(str(expected)):
            return True
        if expected_parent.resolve(strict=False) != expected_parent:
            return True
        local_directory = root / ".local"
        local_info = local_directory.stat()
        if not stat.S_ISDIR(local_info.st_mode):
            return True
        if os.name != "nt" and (
            local_info.st_uid != os.getuid() or stat.S_IMODE(local_info.st_mode) & 0o022
        ):
            return True
        parent_info = expected_parent.stat()
        if not stat.S_ISDIR(parent_info.st_mode):
            return True
        if os.name != "nt" and (
            parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077
        ):
            return True
        marker_info = expected.lstat()
        if not stat.S_ISREG(marker_info.st_mode):
            return True
        if os.name != "nt" and (
            marker_info.st_uid != os.getuid() or stat.S_IMODE(marker_info.st_mode) & 0o077
        ):
            return True
        return True
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError):
        # An unreadable configured marker path cannot be treated as open for writes.
        return True


def maintenance_snapshot():
    with _lock:
        active = _active_mutations
        requests = _active_requests
    return {
        "maintenance": marker_present(),
        "active_requests": requests,
        "active_mutations": active,
    }


class MaintenanceGate:
    """Block new mutating API calls after marker creation and expose a drain count."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        global _active_mutations, _active_requests
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method", "").upper()
        path = scope.get("path", "")
        is_api = path.startswith("/api/v1/")
        is_health = path == "/health" and method in ("GET", "HEAD")
        if not (is_api or is_health):
            return await self.app(scope, receive, send)
        if marker_present():
            if is_health:
                scope.setdefault("state", {})["maintenance_gate_active"] = True
                return await self.app(scope, receive, send)
            return await self._blocked(send)
        with _lock:
            _active_requests += 1
            is_mutation = is_api and method in _WRITE_METHODS
            if is_mutation:
                _active_mutations += 1
        try:
            # Close the small check/increment race: if the supervisor placed the
            # marker between the first check and admission, do not dispatch.
            if marker_present():
                if is_health:
                    scope.setdefault("state", {})["maintenance_gate_active"] = True
                    return await self.app(scope, receive, send)
                return await self._blocked(send)
            return await self.app(scope, receive, send)
        finally:
            with _lock:
                _active_requests -= 1
                if is_api and method in _WRITE_METHODS:
                    _active_mutations -= 1

    @staticmethod
    async def _blocked(send):
        body = json.dumps({"detail": "MAINTENANCE_QUIESCE"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                    (b"pragma", b"no-cache"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
