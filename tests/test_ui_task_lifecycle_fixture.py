from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_fixture_serves_real_task_routes_and_reinitializes_safely(tmp_path: Path) -> None:
    data_dir = (tmp_path / "fixture-data").resolve()
    env = os.environ.copy()
    env["MEWCODE_UI_TASK_LIFECYCLE_DATA_DIR"] = str(data_dir)
    env["MEWCODE_USER_DATA_DIR"] = str(data_dir / "user-data")
    env["MEWCODE_DESKTOP_TOKEN"] = "ui-fixture-test-token"
    script = textwrap.dedent(
        r"""
        import asyncio
        import importlib.util
        import json
        import os
        import socket
        from pathlib import Path

        import httpx

        socket_connect = socket.socket.connect
        socket_connect_ex = socket.socket.connect_ex

        def guard_connect(sock, address):
            if (isinstance(address, tuple) and len(address) >= 2
                    and address[0] in {"127.0.0.1", "::1", "localhost"}
                    and int(address[1]) > 1024):
                return socket_connect(sock, address)
            raise AssertionError("fixture attempted a non-loopback or provider-port connection")

        def guard_connect_ex(sock, address):
            if (isinstance(address, tuple) and len(address) >= 2
                    and address[0] in {"127.0.0.1", "::1", "localhost"}
                    and int(address[1]) > 1024):
                return socket_connect_ex(sock, address)
            raise AssertionError("fixture attempted a non-loopback or provider-port connection")

        def deny_create_connection(*_args, **_kwargs):
            raise AssertionError("fixture must not open a network connection")

        socket.socket.connect = guard_connect
        socket.socket.connect_ex = guard_connect_ex
        socket.create_connection = deny_create_connection

        root = Path.cwd()
        fixture_path = root / "tests" / "ui_task_lifecycle_fixture.py"
        spec = importlib.util.spec_from_file_location("ui_task_fixture", fixture_path)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)

        async def verify():
            data_dir = Path(os.environ["MEWCODE_UI_TASK_LIFECYCLE_DATA_DIR"])
            first = await fixture.initialize_fixture(data_dir)
            first_manager = fixture._server.SESSION.task_manager
            second = await fixture.initialize_fixture(data_dir)
            server = fixture._server
            manager = server.SESSION.task_manager

            assert first["running_task_id"] != second["running_task_id"]
            assert first_manager.get(first["running_task_id"]).status == "cancelled"
            assert first_manager.get(first["group_sibling_task_id"]).status == "cancelled"
            assert first_manager.get(first["queued_task_id"]).status == "cancelled"
            assert Path(server.USER_DATA_ROOT).resolve() == (data_dir / "user-data").resolve()
            assert Path(server.STATE_FILE).resolve() == (data_dir / "workspace" / ".mewcode" / "web_state.json").resolve()
            assert Path.home().resolve() == (data_dir / "home").resolve()
            assert Path(os.environ["APPDATA"]).is_relative_to(data_dir)
            assert Path(os.environ["LOCALAPPDATA"]).is_relative_to(data_dir)
            assert server.DESKTOP_TOKEN == "ui-fixture-test-token"
            assert server.SESSION.client is None
            assert server.SESSION.agent is None
            assert server.SESSION.slots == []
            assert type(manager.get(second["running_task_id"]).agent) is fixture._FixtureAgent
            assert second["task_ids"] == {
                "running_agent": second["running_task_id"],
                "same_parent_queued_agent": second["group_sibling_task_id"],
                "other_parent_queued_agent": second["queued_task_id"],
                "legacy_history_task": second["history_task_id"],
            }
            assert second["task_id_list"] == [
                second["running_task_id"], second["group_sibling_task_id"],
                second["queued_task_id"], second["history_task_id"],
            ]
            assert second["task_groups"][0]["cancellable_task_ids"] == [
                second["running_task_id"], second["group_sibling_task_id"],
            ]

            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
                boot = await client.get(
                    "/desktop-boot?session=ui-fixture-test-token&next=/",
                    follow_redirects=False,
                )
                assert boot.status_code == 302
                assert boot.headers["location"] == "/"
                assert "mewcode_desktop_session=ui-fixture-test-token" in boot.headers["set-cookie"]

                listed = await client.get("/api/tasks")
                assert listed.status_code == 200
                tasks = {item["id"]: item for item in listed.json()["items"]}
                assert tasks[second["running_task_id"]]["status"] == "running"
                assert tasks[second["queued_task_id"]]["status"] == "queued"
                assert tasks[second["group_sibling_task_id"]]["status"] == "queued"
                assert tasks[second["queued_task_id"]]["elapsed"] == 0
                assert tasks[second["queued_task_id"]]["runtime_started"] is False
                assert tasks[second["group_sibling_task_id"]]["elapsed"] == 0
                assert tasks[second["group_sibling_task_id"]]["runtime_started"] is False
                assert tasks[second["history_task_id"]]["status"] == "completed"
                assert tasks[second["history_task_id"]]["elapsed"] is None
                assert tasks[second["history_task_id"]]["legacy_wall_elapsed"] >= 3540

                telemetry_response = await client.get("/api/agent_tasks")
                assert telemetry_response.status_code == 200
                telemetry = telemetry_response.json()
                assert telemetry["conversation_id"] == second["session_id"]
                assert telemetry["running_total"] == 1
                assert telemetry["queued_total"] == 2
                polled_tasks = {item["id"]: item for item in telemetry["tasks"]}
                assert polled_tasks[second["queued_task_id"]]["status"] == "queued"
                assert polled_tasks[second["queued_task_id"]]["elapsed"] == 0
                assert polled_tasks[second["group_sibling_task_id"]]["status"] == "queued"
                assert polled_tasks[second["group_sibling_task_id"]]["elapsed"] == 0
                groups = {group["title"]: group for group in telemetry["groups"]}
                running_group = groups["任务 · Fixture parent: running sample"]
                queued_group = groups["任务 · Fixture parent: queued sample"]
                running_row = next(row for row in running_group["rows"]
                                   if row["task_id"] == second["running_task_id"])
                queued_row = next(row for row in queued_group["rows"]
                                  if row["task_id"] == second["queued_task_id"])
                same_group_queued_row = next(row for row in running_group["rows"]
                                             if row["task_id"] == second["group_sibling_task_id"])
                history_row = next(row for row in running_group["rows"]
                                   if row["task_id"] == second["history_task_id"])
                assert {
                    row["task_id"] for row in running_group["rows"]
                    if row["status"] in {"queued", "running"}
                } == {second["running_task_id"], second["group_sibling_task_id"]}
                assert running_row["status"] == "running"
                assert running_row["runtime_active"] is True
                assert running_row["runtime_active_slots"] == 1
                assert running_row["duration_basis"] == "active_runtime"
                assert queued_row["status"] == "queued"
                assert queued_row["queue_position"] == 2
                assert queued_row["elapsed"] == 0
                assert same_group_queued_row["status"] == "queued"
                assert same_group_queued_row["queue_position"] == 1
                assert same_group_queued_row["elapsed"] == 0
                assert history_row["duration_basis"] == "legacy_wall"
                assert history_row["duration_quality"] == "unknown"
                assert history_row["elapsed"] >= 3540

                detail = await client.get("/api/tasks/" + second["history_task_id"])
                assert detail.status_code == 200
                assert detail.json()["duration_basis"] == "legacy_wall"
                assert detail.json()["elapsed"] is None
                assert detail.json()["legacy_wall_elapsed"] >= 3540

                cancelled = await client.post(
                    "/api/tasks/" + second["running_task_id"] + "/cancel"
                )
                assert cancelled.status_code == 200
                assert cancelled.json()["cancelled"] is True
                assert cancelled.json()["status"] == "cancelled"
                task = manager.get(second["running_task_id"])
                assert task is not None
                assert task.status == "cancelled"
                assert task.lifecycle_finalized is True
                assert task.cleanup_confirmed is True

                after = await client.get(
                    "/api/tasks/" + second["running_task_id"]
                )
                assert after.status_code == 200
                assert after.json()["status"] == "cancelled"
                polled = (await client.get("/api/agent_tasks")).json()
                sidebar = {item["id"]: item for item in polled["tasks"]}
                assert sidebar[second["running_task_id"]]["status"] == "cancelled"
                sibling_agent = manager.get(second["group_sibling_task_id"]).agent
                await asyncio.wait_for(sibling_agent.started.wait(), timeout=3.0)
                after_single = await client.get("/api/agent_tasks")
                after_single_groups = {
                    group["title"]: group for group in after_single.json()["groups"]
                }
                same_parent_rows = {
                    row["task_id"]: row
                    for row in after_single_groups["任务 · Fixture parent: running sample"]["rows"]
                }
                other_parent_rows = {
                    row["task_id"]: row
                    for row in after_single_groups["任务 · Fixture parent: queued sample"]["rows"]
                }
                assert same_parent_rows[second["running_task_id"]]["status"] == "cancelled"
                assert same_parent_rows[second["group_sibling_task_id"]]["status"] == "running"
                assert other_parent_rows[second["queued_task_id"]]["status"] == "queued"

                # A fresh generation lets the test exercise the group-stop
                # action independently. The real renderer gathers the active
                # row task IDs from one group, then posts each single-item route.
                third = await fixture.initialize_fixture(data_dir)
                third_manager = server.SESSION.task_manager
                assert manager.get(second["running_task_id"]).status == "cancelled"
                assert manager.get(second["group_sibling_task_id"]).status == "cancelled"
                assert manager.get(second["queued_task_id"]).status == "cancelled"

                grouped = (await client.get("/api/agent_tasks")).json()
                group = next(item for item in grouped["groups"]
                             if item["title"] == "任务 · Fixture parent: running sample")
                group_task_ids = [
                    row["task_id"] for row in group["rows"]
                    if row["status"] in {"queued", "running"}
                    and not row["cancellation_pending"] and row["task_id"]
                ]
                assert set(group_task_ids) == {
                    third["running_task_id"], third["group_sibling_task_id"],
                }
                group_cancel_results = await asyncio.gather(*(
                    client.post("/api/tasks/" + task_id + "/cancel")
                    for task_id in group_task_ids
                ))
                assert all(response.status_code == 200 for response in group_cancel_results)
                assert all(response.json()["status"] == "cancelled"
                           for response in group_cancel_results)
                assert third_manager.get(third["running_task_id"]).status == "cancelled"
                assert third_manager.get(third["group_sibling_task_id"]).status == "cancelled"
                assert third_manager.get(third["queued_task_id"]).status != "cancelled"
                after_group = (await client.get("/api/agent_tasks")).json()
                remaining_groups = {item["title"]: item for item in after_group["groups"]}
                other_group = remaining_groups["任务 · Fixture parent: queued sample"]
                assert [row["task_id"] for row in other_group["rows"]] == [
                    third["queued_task_id"]
                ]
                assert other_group["rows"][0]["status"] in {"queued", "running"}

            await fixture._stop_fixture()
            manifest = json.loads((data_dir / "fixture-manifest.json").read_text(encoding="utf-8"))
            assert manifest["running_task_id"] == third["running_task_id"]
            assert manifest["task_ids"]["same_parent_queued_agent"] == third["group_sibling_task_id"]
            assert manifest["task_groups"][0]["cancellable_task_ids"] == [
                third["running_task_id"], third["group_sibling_task_id"],
            ]
            assert all(third_manager.get(task_id).status == "cancelled"
                       for task_id in third["task_id_list"][:3])

        asyncio.run(verify())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, (
        f"fixture subprocess failed ({result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
