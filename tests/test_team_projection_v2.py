from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from frontend.team_v2_api import install_team_v2_api
from mewcode.teams.host import TeamHost, template


@pytest.fixture
def web_case(tmp_path):
    host = TeamHost(tmp_path)
    app = FastAPI()
    install_team_v2_api(app, host, authorize=lambda request: request.headers.get('authorization') == 'Bearer local-test',
                        static_dir=Path(__file__).resolve().parents[1] / 'frontend' / 'static')
    with TestClient(app, headers={'authorization': 'Bearer local-test', 'X-Team-V2': '1'}) as client:
        yield client, host
    host.close()


def create_draft(client, domain='software'):
    assert client.post('/api/team-v2/enable', json={'enabled': True}).status_code == 200
    response = client.post('/api/team-v2/drafts', json=dict(definition=template(domain), name=domain, objective='A bounded delivery', budget_ref=domain + '-budget', command_id=domain + '-draft'))
    assert response.status_code == 200, response.text
    return response.json()['run']['team_run_id']


def test_disabled_is_read_only_and_never_migrates(web_case):
    client, host = web_case
    assert client.get('/api/team-v2/status').json()['enabled'] is False
    assert not (host.state / 'coordination.sqlite3').exists()
    assert client.get('/api/team-v2/runs').status_code == 409
    assert client.post('/api/team-v2/enable', json={'enabled': False}).status_code == 422


def test_desktop_authorization_and_csrf_are_required(web_case):
    client, host = web_case
    assert client.get('/api/team-v2/status', headers={'authorization': ''}).status_code == 403
    assert client.post('/api/team-v2/enable', json={'enabled': True}, headers={'origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/team-v2/enable', json={'enabled': True}, headers={'X-Team-V2': ''}).status_code == 403
    assert not host.enabled


def test_draft_projection_has_no_execution_or_lease_secrets(web_case):
    client, host = web_case
    run_id = create_draft(client)
    view = client.get('/api/team-v2/runs/' + run_id).json()
    assert view['run']['state'] == 'draft' and view['tasks'] == []
    assert view['execution_slots'] == 1 and view['definition']['tasks']
    assert 'lease_token' not in str(view)
    assert client.get('/api/team-v2/runs/' + run_id + '/activation-plan').json()['minimum_budget'] == 8000
    assert client.get('/teams').status_code == 200


def test_http_cannot_spoof_an_executor_or_workspace(web_case):
    client, _ = web_case
    run_id = create_draft(client)
    route = '/api/team-v2/runs/' + run_id + '/commands'
    assert client.post(route, json=dict(command='claim', arguments={})).status_code == 409
    assert client.post(route, json=dict(command='cancel_run', arguments={'actor': 'grok-executor-01'})).status_code == 409
    assert client.post(route, json=dict(command='cancel_run', arguments={'run_id': 'some-other-run'})).status_code == 409


def test_draft_idempotence_and_composition_preview(web_case):
    client, host = web_case
    run_id = create_draft(client)
    assert create_draft(client) == run_id
    definition = template()
    response = client.post('/api/team-v2/compose', json=dict(spec_id='serial-team', version=1,
                           components={'first': definition, 'second': definition}, inputs=definition['inputs'], outputs=definition['outputs'],
                           links=[dict(source_component=None, source_port='request', target_component='first', target_port='request'),
                                  dict(source_component='first', source_port='delivery', target_component='second', target_port='request'),
                                  dict(source_component='second', source_port='delivery', target_component=None, target_port='delivery')], policy=definition['policy']))
    # Two 8k components cannot magically multiply the 10k common grant.
    assert response.status_code == 422 and 'BUDGET_EXCEEDED' in response.text
