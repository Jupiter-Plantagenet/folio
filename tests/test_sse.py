from fastapi.testclient import TestClient
from test_domain_completion import session


def test_durable_sse_replay_only_returns_events_after_cursor_and_denies_foreign_scope():
    from folio.workbench import WORKSPACE, app, create_case, migrate

    migrate()
    client = TestClient(app)
    session(client, "viewer")
    create_case()
    url = f"/api/v1/workspaces/{WORKSPACE}/events?follow=false"
    first = client.get(url)
    assert first.status_code == 200 and first.headers["content-type"].startswith(
        "text/event-stream"
    )
    ids = [int(line[4:]) for line in first.text.splitlines() if line.startswith("id: ")]
    assert ids and len(ids) == len(set(ids))
    create_case()
    replay = client.get(url, headers={"Last-Event-ID": str(ids[-1])})
    later = [
        int(line[4:]) for line in replay.text.splitlines() if line.startswith("id: ")
    ]
    assert later and all(n > ids[-1] for n in later)
    assert client.get(url, headers={"Last-Event-ID": "../invalid"}).status_code == 422
    assert (
        client.get(
            "/api/v1/workspaces/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/events?follow=false"
        ).status_code
        == 404
    )
