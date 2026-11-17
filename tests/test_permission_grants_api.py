"""Persistent grant listing and revocation are scoped to the selected project."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from frontend import server
from mewcode.permissions.grants import GrantKind, GrantStore


class RequestStub:
    def __init__(self, payload: dict, *, confirmed: bool = True):
        self.headers = {"X-Mew-User-Grant": "1"} if confirmed else {}
        self.payload = payload

    async def json(self):
        return self.payload


@pytest.mark.asyncio
async def test_project_grants_can_be_listed_and_revoked_only_for_current_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    directory = tmp_path / "materials"
    directory.mkdir()
    store = GrantStore(project)
    preview = store.preview_scope(GrantKind.READ_DIRECTORY, str(directory), 3600)
    grant = store.grant(GrantKind.READ_DIRECTORY, str(directory), ttl_seconds=3600,
                        confirmation={"confirmed": True, "actor": "user",
                                      "confirmation_id": "confirm-api-0001", "confirmed_at": time.time(),
                                      "scope": preview})
    monkeypatch.setattr(server, "SESSION", SimpleNamespace(work_dir=str(project)))

    listed = await server.api_project_permission_grants()
    payload = json.loads(listed.body)
    assert listed.status_code == 200
    assert [row["grant_id"] for row in payload["grants"]] == [grant.grant_id]

    missing_confirmation = await server.api_project_permission_grant_revoke(
        RequestStub({"project": str(project), "grant_id": grant.grant_id}, confirmed=False))
    assert missing_confirmation.status_code == 403
    wrong_project = await server.api_project_permission_grant_revoke(
        RequestStub({"project": str(directory), "grant_id": grant.grant_id}))
    assert wrong_project.status_code == 409
    revoked = await server.api_project_permission_grant_revoke(
        RequestStub({"project": str(project.resolve()), "grant_id": grant.grant_id}))
    assert revoked.status_code == 200
    assert json.loads((await server.api_project_permission_grants()).body)["grants"] == []
