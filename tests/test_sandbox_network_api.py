from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import frontend.sandbox_network as api
from mewcode.permissions.network_policy import NetworkPolicyStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = NetworkPolicyStore(tmp_path / 'network.json')
    monkeypatch.setattr(api, 'NetworkPolicyStore', lambda: store)
    monkeypatch.setattr(api, 'runtime_status', lambda: {'ready': False, 'state': 'pending'})
    app = FastAPI()
    session = SimpleNamespace(session=SimpleNamespace(session_id='c1'))
    async def run_control(operation):
        return operation()
    api.register_network_routes(app, lambda: session, run_control)
    return TestClient(app), store, session


def test_network_state_distinguishes_setting_and_runtime(client):
    c, _, _ = client
    state = c.get('/api/chat/sandbox-network').json()
    assert state['policy']['enabled']
    assert state['effective']['structured_tools_enabled']
    assert not state['effective']['command_network_ready']


def test_auto_network_does_not_depend_on_native_readiness(client, tmp_path):
    from mewcode.permissions.policy import build_permission_checker
    c, store, session = client
    checker = build_permission_checker(tmp_path)
    checker.network_policy_store = store
    session.agent = SimpleNamespace(permission_checker=checker)
    state = c.get('/api/chat/sandbox-network').json()
    assert state['effective']['execution_backend'] == 'host_reviewed'
    assert state['effective']['command_network_ready'] and not state['runtime']['ready']
    store.save({'enabled': True, 'denied_domains': ['example.test']}, expected_revision=store.read().revision)
    restricted = c.get('/api/chat/sandbox-network').json()
    assert restricted['effective']['execution_backend'] == 'native'
    assert not restricted['effective']['command_network_ready']


def test_network_update_requires_ui_header_and_fresh_version(client):
    c, store, _ = client
    data = {'conversation_id': 'c1', 'enabled': False, 'denied_domains': ['EXAMPLE.COM.'],
            'revision': store.read().revision}
    assert c.post('/api/chat/sandbox-network', json=data).status_code == 403
    headers = {'X-Mew-Sandbox-Control': '1'}
    result = c.post('/api/chat/sandbox-network', json=data, headers=headers)
    assert result.status_code == 200
    assert not result.json()['policy']['enabled']
    assert store.read().denied_domains == ('example.com',)
    assert c.post('/api/chat/sandbox-network', json=data, headers=headers).status_code == 409


def test_old_conversation_cannot_change_global_policy(client):
    c, store, session = client
    session.session.session_id = 'c2'
    data = {'conversation_id': 'c1', 'enabled': False, 'revision': store.read().revision}
    assert c.post('/api/chat/sandbox-network', json=data,
                  headers={'X-Mew-Sandbox-Control': '1'}).status_code == 409
    assert store.read().enabled


def test_configuration_never_runs_on_read_and_requires_ui_action(client, monkeypatch):
    c, _, _ = client
    called = Mock(return_value={'ready': False, 'cases': {}})
    monkeypatch.setattr('mewcode.sandbox.srt_setup.configure_and_verify', called)
    c.get('/api/chat/sandbox-network')
    called.assert_not_called()
    assert c.post('/api/chat/sandbox-network/configure', json={'conversation_id': 'c1'}).status_code == 403
    assert c.post('/api/chat/sandbox-network/configure', json={'conversation_id': 'c1'},
                  headers={'X-Mew-Sandbox-Control': '1'}).status_code == 200
    called.assert_called_once()
