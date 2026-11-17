"""Exercise the production desktop security boundary without a live backend."""
import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
import frontend.server as desktop


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(desktop, 'DESKTOP_TOKEN', 'isolated-security-test')
    app = FastAPI()
    app.middleware('http')(desktop.desktop_security)
    app.add_api_route('/desktop-boot', desktop.desktop_boot, methods=['GET'])
    for route in ['/teams', '/orch-canvas', '/', '/pipeline', '/teams-extra', '/workbench/connections/' + 'a' * 32]:
        app.add_api_route(route, lambda: HTMLResponse('fixture'), methods=['GET'])
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize('cookie', [None, 'stale-token'])
def test_workspace_rejects_missing_or_stale_session(client, cookie):
    if cookie:
        client.cookies.set(desktop.DESKTOP_COOKIE, cookie)
    response = client.get('/teams?embedded=1', headers={'Accept': 'text/html', 'Origin': 'http://testserver', 'Referer': 'http://testserver/'})
    assert response.status_code == 401
    assert '此页面未收到有效的桌面登录会话' in response.text
    assert 'mewcode:team-workspace:close' not in response.text
    assert 'orch-canvas-close' not in response.text
    assert response.headers['cache-control'] == 'no-store'


def test_bootstrap_cookie_authenticates_without_legacy_frame_exceptions(client):
    boot = client.get('/desktop-boot?session=isolated-security-test&next=/teams?embedded=1', follow_redirects=False)
    assert boot.status_code == 302
    assert 'HttpOnly' in boot.headers['set-cookie']
    assert 'SameSite=strict' in boot.headers['set-cookie']
    for route in ['/teams?embedded=1', '/orch-canvas', '/', '/pipeline', '/teams-extra']:
        response = client.get(route)
        assert response.status_code == 200
        assert response.headers['x-frame-options'] == 'DENY'
        assert "frame-ancestors 'none'" in response.headers['content-security-policy']


def test_bad_bootstrap_cannot_authenticate_workspace(client):
    client.get('/desktop-boot?session=wrong&next=/teams')
    assert client.get('/teams').status_code == 401


def test_only_authenticated_same_origin_connection_page_can_be_embedded(client):
    path = '/workbench/connections/' + 'a' * 32
    assert client.get(path).status_code == 401
    client.get('/desktop-boot?session=isolated-security-test&next=/')
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers['x-frame-options'] == 'SAMEORIGIN'
    assert "frame-ancestors 'self'" in response.headers['content-security-policy']
