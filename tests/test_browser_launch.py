from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
import json
import pytest

from frontend.browser_launch import BrowserLaunches
import frontend.server as desktop


def test_handoff_expires_and_cannot_be_replayed():
    now = [100]
    launches = BrowserLaunches(ttl=90, clock=lambda: now[0])
    ticket = launches.issue().split("=", 1)[1]
    assert launches.consume(ticket)
    assert not launches.consume(ticket)
    ticket = launches.issue().split("=", 1)[1]
    now[0] = 190
    assert not launches.consume(ticket)


def test_launcher_file_is_single_use_and_does_not_replace_existing_file(tmp_path):
    launches = BrowserLaunches()
    path = tmp_path / "entry.json"
    launches.publish_startup(path, 8765)
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["expires_in"] == 90
    assert entry["url"].startswith("http://127.0.0.1:8765/desktop-boot?ticket=")
    assert launches.consume(entry["url"].split("=", 1)[1])
    with pytest.raises(FileExistsError):
        launches.publish_startup(path, 9999)
    assert json.loads(path.read_text(encoding="utf-8")) == entry


def test_browser_handoff_requires_authenticated_same_origin_client(monkeypatch):
    monkeypatch.setattr(desktop, "DESKTOP_TOKEN", "test-desktop-secret")
    monkeypatch.setattr(desktop, "BROWSER_LAUNCHES", BrowserLaunches())
    app = FastAPI()
    app.middleware("http")(desktop.desktop_security)
    app.add_api_route("/desktop-boot", desktop.desktop_boot)
    app.add_api_route("/api/browser-launch", desktop.api_browser_launch, methods=["POST"])
    app.add_api_route("/", lambda: HTMLResponse("workspace"))
    with TestClient(app) as client:
        assert client.post("/api/browser-launch").status_code == 401
        client.get("/desktop-boot?session=test-desktop-secret")
        assert client.post("/api/browser-launch", headers={"Origin": "https://other.example"}).status_code == 403
        response = client.post("/api/browser-launch", headers={"Origin": "http://testserver"})
        assert response.status_code == 200
        path = response.json()["path"]
        assert "test-desktop-secret" not in path
        with TestClient(app) as browser:
            assert browser.get("/").status_code == 401
            boot = browser.get(path, follow_redirects=False)
            assert boot.status_code == 302
            assert "HttpOnly" in boot.headers["set-cookie"]
            assert browser.get("/").status_code == 200
        with TestClient(app) as replay:
            replay.get(path)
            assert replay.get("/").status_code == 401
