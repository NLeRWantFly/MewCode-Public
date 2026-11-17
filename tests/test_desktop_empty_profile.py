"""The real frontend must import with an empty first-run profile, not credentials."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_empty_profile_import_serves_health_and_isolates_team_state(tmp_path):
    root = Path(__file__).resolve().parents[1]
    data = tmp_path / "desktop-data"
    data.mkdir()
    env = os.environ.copy()
    env.update(MEWCODE_USER_DATA_DIR=str(data), PYTHONPATH=str(root),
               USERPROFILE=str(tmp_path), HOME=str(tmp_path),
               APPDATA=str(tmp_path / "roaming"), LOCALAPPDATA=str(tmp_path / "local"),
               MEWCODE_DESKTOP_TOKEN="empty-profile-test", MEWCODE_DESKTOP_PROFILE_ISOLATED="1")
    script = textwrap.dedent('''
        import asyncio
        import socket
        from pathlib import Path
        import httpx
        from mewcode.config import ConfigError, load_config

        connect = socket.socket.connect
        connect_ex = socket.socket.connect_ex
        def local_only(original):
            def guarded(sock, address):
                # Windows asyncio builds its self-pipe from loopback sockets.
                if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1'} and address[1] > 1024:
                    return original(sock, address)
                return deny_network()
            return guarded
        def deny_network(*args, **kwargs):
            raise AssertionError("empty-profile import must not make network calls")
        socket.socket.connect = local_only(connect)
        socket.socket.connect_ex = local_only(connect_ex)
        try:
            load_config()
        except ConfigError:
            pass
        else:
            raise AssertionError("test must start without config")

        from frontend import server
        host = server.app.state.team_v2_host
        assert host.root == Path.cwd()
        assert not host.enabled
        assert host.provider_configs == ()
        assert not (host.root / '.mewcode' / 'coordination').exists()
        assert server.SESSION.agent is None

        async def check():
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1',
                                         headers={'Authorization': 'Bearer empty-profile-test'}) as client:
                response = await client.get('/api/health')
                assert response.status_code == 200, response.text
                response = await client.get('/api/team-v2/status')
                assert response.status_code == 200, response.text
                assert response.json()['enabled'] is False
        asyncio.run(check())
        print('empty-profile: health 200, no team routes activated, isolated state')
    ''')
    result = subprocess.run([sys.executable, "-c", script], cwd=data, env=env,
                            text=True, encoding="utf-8", errors="replace",
                            capture_output=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
