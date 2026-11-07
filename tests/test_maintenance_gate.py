import asyncio
import os
import stat

from folio import maintenance_gate


def request_scope(method="POST", path="/api/v1/workspaces/w/cases"):
    return {"type": "http", "method": method, "path": path}


def test_marker_blocks_new_mutations_but_not_read_only_calls(tmp_path, monkeypatch):
    product = tmp_path / "folio-product"
    package = product / "folio"
    marker_dir = product / ".local" / "maintenance"
    package.mkdir(parents=True)
    marker_dir.mkdir(parents=True)
    marker = marker_dir / "folio-quiesce.json"
    if os.name != "nt":
        marker_dir.chmod(stat.S_IRWXU)
    monkeypatch.setattr(maintenance_gate, "__file__", str(package / "maintenance_gate.py"))
    monkeypatch.setenv("FOLIO_MAINTENANCE_MARKER", str(marker))

    dispatched = []

    async def app(scope, _receive, send):
        dispatched.append((scope["method"], scope["path"], scope.get("state", {}).get("maintenance_gate_active")))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def call(method, path):
        responses = []

        async def send(message):
            responses.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await maintenance_gate.MaintenanceGate(app)(request_scope(method, path), receive, send)
        return responses

    async def run():
        marker.write_text("opaque operator marker", encoding="utf-8")
        if os.name != "nt":
            marker.chmod(stat.S_IRUSR | stat.S_IWUSR)
        blocked = await call("POST", "/api/v1/workspaces/w/cases")
        assert blocked[0]["status"] == 503
        assert b"MAINTENANCE_QUIESCE" in blocked[1]["body"]
        assert not dispatched
        blocked_read = await call("GET", "/api/v1/workspaces/w/cases")
        assert blocked_read[0]["status"] == 503
        assert not dispatched
        assert (await call("GET", "/health"))[0]["status"] == 200
        assert len(dispatched) == 1
        assert dispatched[0] == ("GET", "/health", True)
        assert maintenance_gate.maintenance_snapshot()["maintenance"] is True
        assert maintenance_gate.maintenance_snapshot()["active_requests"] == 0
        assert maintenance_gate.maintenance_snapshot()["active_mutations"] == 0

    asyncio.run(run())


def test_active_counter_covers_the_request_until_response_completion(tmp_path, monkeypatch):
    product = tmp_path / "folio-product"
    package = product / "folio"
    marker_dir = product / ".local" / "maintenance"
    package.mkdir(parents=True)
    marker_dir.mkdir(parents=True)
    marker = marker_dir / "folio-quiesce.json"
    if os.name != "nt":
        marker_dir.chmod(stat.S_IRWXU)
    monkeypatch.setattr(maintenance_gate, "__file__", str(package / "maintenance_gate.py"))
    monkeypatch.setenv("FOLIO_MAINTENANCE_MARKER", str(marker))
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def app(_scope, _receive, send):
        entered.set()
        await finish.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"committed"})

    async def run():
        responses = []

        async def send(message):
            responses.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        task = asyncio.create_task(
            maintenance_gate.MaintenanceGate(app)(request_scope(), receive, send)
        )
        await entered.wait()
        assert maintenance_gate.maintenance_snapshot()["active_requests"] == 1
        assert maintenance_gate.maintenance_snapshot()["active_mutations"] == 1
        marker.write_text("opaque operator marker", encoding="utf-8")
        if os.name != "nt":
            marker.chmod(stat.S_IRUSR | stat.S_IWUSR)
        assert maintenance_gate.maintenance_snapshot()["maintenance"] is True
        finish.set()
        await task
        assert responses[-1]["body"] == b"committed"
        assert maintenance_gate.maintenance_snapshot()["active_requests"] == 0
        assert maintenance_gate.maintenance_snapshot()["active_mutations"] == 0

    asyncio.run(run())


def test_inflight_read_is_counted_for_quiescence(tmp_path, monkeypatch):
    product = tmp_path / "folio-product"
    package = product / "folio"
    marker_dir = product / ".local" / "maintenance"
    package.mkdir(parents=True)
    marker_dir.mkdir(parents=True)
    marker = marker_dir / "folio-quiesce.json"
    if os.name != "nt":
        marker_dir.chmod(stat.S_IRWXU)
    monkeypatch.setattr(maintenance_gate, "__file__", str(package / "maintenance_gate.py"))
    monkeypatch.setenv("FOLIO_MAINTENANCE_MARKER", str(marker))
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def app(_scope, _receive, send):
        entered.set()
        await finish.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"read"})

    async def run():
        async def send(_message):
            return None

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        task = asyncio.create_task(
            maintenance_gate.MaintenanceGate(app)(
                request_scope("GET", "/api/v1/workspaces/w/activity"), receive, send
            )
        )
        await entered.wait()
        state = maintenance_gate.maintenance_snapshot()
        assert state["active_requests"] == 1
        assert state["active_mutations"] == 0
        marker.write_text("opaque operator marker", encoding="utf-8")
        if os.name != "nt":
            marker.chmod(stat.S_IRUSR | stat.S_IWUSR)
        finish.set()
        await task
        assert maintenance_gate.maintenance_snapshot()["active_requests"] == 0

    asyncio.run(run())


def test_health_short_circuits_before_any_database_probe(monkeypatch):
    from starlette.requests import Request

    from folio import workbench

    monkeypatch.setattr(
        workbench,
        "maintenance_snapshot",
        lambda: {"maintenance": True, "active_requests": 4, "active_mutations": 1},
    )
    monkeypatch.setattr(workbench, "pooled", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("DB probe")))
    request = Request(
        {
            "type": "http", "method": "GET", "path": "/health", "headers": [],
            "query_string": b"", "state": {}, "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
        }
    )
    response = workbench.health(request)
    assert response.status_code == 200
    assert b"not_checked_during_maintenance" in response.body
    assert b"active_requests" in response.body
