"""Real API/PostgreSQL intake proof; authored documents and disposable profiles only."""
import io
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


def _client():
    from test_domain_completion import session

    from folio import workbench
    if not urlsplit(workbench.DSN).path.lstrip('/').startswith('folio_finish_wb_'):
        pytest.skip('Requires an isolated disposable Folio proof database')
    workbench.migrate()
    client = TestClient(workbench.app)
    return client, session(client), workbench.WORKSPACE


def _pdf():
    from reportlab.pdfgen.canvas import Canvas
    out = io.BytesIO()
    canvas = Canvas(out, pagesize=(350, 200))
    canvas.drawString(20, 150, 'Authored invoice multipart capacity proof')
    canvas.drawString(20, 120, 'Invoice INV-MULTIPART-20M')
    canvas.save()
    raw = out.getvalue()
    return raw + b'\n%' + b'x' * (20 * 1024**2 - len(raw) - 2)


def _parts(raw):
    return [('rights_attestation', (None, 'authored-synthetic')),
            ('document_roles', (None, 'invoice')),
            ('files', ('authored-boundary.pdf', raw, 'application/pdf'))]


def test_real_multipart_20mib_persists_and_replays_same_command():
    client, headers, workspace = _client()
    raw = _pdf()
    assert len(raw) == 20 * 1024**2
    url = f'/api/v1/workspaces/{workspace}/intakes/files'
    headers = {**headers, 'Idempotency-Key': str(uuid4())}
    try:
        first = client.post(url, headers=headers, files=_parts(raw))
        assert first.status_code == 202, first.status_code
        assert first.json()['case_id']
        replay = client.post(url, headers=headers, files=_parts(raw))
        assert replay.status_code == 202, replay.status_code
        assert replay.json() == first.json()
        case = client.get(f'/api/v1/workspaces/{workspace}/cases/{first.json()["case_id"]}')
        assert case.status_code == 200
        assert len(case.json()['sources']) == 1
        assert case.json()['sources'][0]['name'] == 'authored-boundary.pdf'
    finally:
        client.close()


def test_real_multipart_one_byte_over_file_limit_is_413():
    client, headers, workspace = _client()
    try:
        response = client.post(f'/api/v1/workspaces/{workspace}/intakes/files',
            headers={**headers, 'Idempotency-Key': str(uuid4())}, files=_parts(_pdf() + b'x'))
        assert response.status_code == 413, response.status_code
        assert response.json()['detail'] == 'FILE_LIMIT'
    finally:
        client.close()
