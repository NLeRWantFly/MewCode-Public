import asyncio
import base64
import copy
import json

import pytest

from mewcode.mcp.coord_worker import GrokCoordWorker
from mewcode.mcp.coord_server import build_server
from test_team_binding_v2 import TeamInvoker
from test_team_projection_v2 import web_case, create_draft


@pytest.mark.parametrize('domain', ['software', 'data', 'research', 'content'])
def test_same_kernel_http_worker_handoff_review_and_export(web_case, domain):
    client, host = web_case
    run_id = create_draft(client, domain)
    route = '/api/team-v2/runs/' + run_id
    sequence = 0
    def command(name, **arguments):
        nonlocal sequence
        sequence += 1
        arguments.update(command_id='e2e-' + str(sequence), control_epoch=1)
        response = client.post(route + '/commands', json=dict(command=name, arguments=arguments))
        assert response.status_code == 200, response.text
        return response.json()
    content = dict(port_id='request', schema_ref=domain + '-artifact/v1', media_type='text/plain',
                   content_b64=base64.b64encode(b'Synthetic fixture only. Retain evidence for the host.').decode())
    seed = command('register_input', content=content)
    plan = client.get(route + '/activation-plan').json()
    command('activate', bindings=plan['bindings'], inputs={'request': seed['version_id']}, budget_limit=8000, expected_version=1)
    invoker = TeamInvoker()
    manifest = dict(agent_id='grok-executor-01', model_id='grok-4.6', role='executor', tools=[], skills=[], mcp_servers=[])
    worker = GrokCoordWorker(root=host.root, store=host._store, invoker=invoker, capabilities=manifest)
    for step in range(2):
        outcomes = asyncio.run(worker.run_once())
        assert len(outcomes) == 1 and outcomes[0]['handoff_id']
        view = client.get(route).json()
        current = next(t for t in view['tasks'] if t['task_id'] == outcomes[0]['task_id'])
        assert current['state'] == 'verifying'
        command('review', handoff_id=outcomes[0]['handoff_id'], expected_revision=current['revision'], accepted=True,
                scores=[dict(criterion_id=c['criterion_id'], score=10000, evidence='Test harness inspected five expected fixture bytes, not a real model result') for c in current['contract']['acceptance']])
        if step == 0:
            view = client.get(route).json()
            consumer = next(t for t in view['tasks'] if t['task_key'] == 'deliver')
            assert consumer['state'] == 'waiting_inputs'
            assert asyncio.run(worker.run_once()) == []
            slot = consumer['inputs'][0]
            command('accept_input', task_id=consumer['task_id'], port_id=slot['port_id'], version_id=slot['version_id'],
                    revision=consumer['revision'], accepted=True, reason='Explicit fixture compatibility receipt')
    view = client.get(route).json()
    assert view['run']['state'] == 'verifying' and view['budget']['used_tokens'] == 84
    result = command('finalize', expected_version=view['run']['state_version'], evidence='All current fixture output versions inspected')
    version = result['manifest']['exports']['delivery']
    artifact = client.get('/api/team-v2/artifacts/' + version).json()
    assert base64.b64decode(artifact['content_b64']) == bytes([0, 1, 2, 3, 255])
    assert len(invoker.requests) == 2 and all(r.data_only for r in invoker.requests)
    assert host._store.list_tasks() == []
    host.close()
    restored = client.get(route).json()
    assert restored['run']['state'] == 'completed' and restored['export']['exports']['delivery'] == version
    # No model turn wrote a formal workspace file.
    assert sorted(p.name for p in host.root.iterdir()) == ['.mewcode']


def test_v2_mcp_is_explicit_and_cannot_execute(web_case):
    client, host = web_case
    create_draft(client)
    server = build_server(host._store, team_host=host)
    names = {t.name for t in server._tool_manager.list_tools()}
    assert 'coord_team_v2_command' in names
    assert 'coord_claim_task' not in names
    fn = server._tool_manager.get_tool('coord_team_v2_command').fn
    run = host.list_runs()[0]['run']['team_run_id']
    with pytest.raises(Exception, match='UNSUPPORTED_COMMAND'):
        fn(run, 'claim', {})


@pytest.mark.parametrize('mapped', [False, True])
def test_single_slot_fanout_or_map_then_required_join(web_case, mapped):
    from mewcode.teams.host import template
    from mewcode.teams.composition import map_team
    from mewcode.teams.runtime_binding import GrokTeamExecutor
    from mewcode.teams.runtime_protocol import ArtifactContent
    client, host = web_case
    host.enable()
    raw = template('data')
    for task in raw['tasks']:
        task['token_budget'] = 1000
    if mapped:
        component = host.service.validate_spec(raw)
        plan = map_team(component, ('left', 'right'), spec_id='map-reduce',
                        policy=component.effective_policy, context=host.service.context)
        raw = plan.compiled.spec.model_dump(mode='json')
        sources = [('left__deliver', 'left'), ('right__deliver', 'right')]
        reducer = copy.deepcopy(raw['tasks'][0])
    else:
        original = copy.deepcopy(raw['tasks'][0])
        raw['tasks'] = [dict(copy.deepcopy(original), task_key=key) for key in ('prepare', 'left', 'right')]
        raw['connections'] = [dict(source=dict(port_id='request'), target=dict(task_key='prepare', port_id='request'))]
        raw['connections'] += [dict(source=dict(task_key='prepare', port_id='result'), target=dict(task_key=key, port_id='request')) for key in ('left', 'right')]
        sources = [('left', 'left'), ('right', 'right')]
        reducer = copy.deepcopy(original)
    raw['spec_id'] = 'map-reduce' if mapped else 'fanout-join'
    raw['outputs'] = [dict(port_id='delivery', schema_ref='data-artifact/v1')]
    raw['connections'] = [edge for edge in raw['connections'] if edge['target'].get('task_key')]
    reducer.update(task_key='reduce', inputs=[dict(port_id=key, schema_ref='data-artifact/v1') for key in ('left', 'right')])
    raw['tasks'].append(reducer)
    raw['connections'] += [dict(source=dict(task_key=key, port_id='result'), target=dict(task_key='reduce', port_id=port)) for key, port in sources]
    raw['connections'].append(dict(source=dict(task_key='reduce', port_id='result'), target=dict(port_id='delivery')))
    run = host.draft(definition=raw, name='graph', objective='Test explicit joins', budget_ref='graph', command_id='graph')['run']['team_run_id']
    rt, owner = host.runtime, host.actor
    inputs = {p['port_id']: rt.register_input(owner, run, ArtifactContent.from_bytes(port_id=p['port_id'], schema_ref=p['schema_ref'],
              media_type='text/plain', content=p['port_id'].encode()), control_epoch=1, command_id='seed-' + p['port_id'])['version_id'] for p in raw['inputs']}
    active = rt.activate(owner, run, bindings=host.activation_plan(run)['bindings'], inputs=inputs, budget_limit=10000,
                         expected_version=1, control_epoch=1, command_id='activate')
    invoker = TeamInvoker()
    executor = GrokTeamExecutor(rt, invoker, root=host.root, session_id='graph')
    receipt_count = 0
    partial_join_checked = False
    for index in range(len(raw['tasks'])):
        result = asyncio.run(executor.run_once())
        assert len(result) == 1  # Logical branches never imply concurrent model calls.
        outcome = result[0]
        task = rt.task(owner, outcome['task_id'])
        rt.review(owner, outcome['handoff_id'], accepted=True, expected_revision=task['revision'], control_epoch=1,
                  command_id='review-' + str(index), scores=[dict(criterion_id=c['criterion_id'], score=10000,
                  evidence='Synthetic graph test inspected exact five-byte artifact') for c in task['contract']['acceptance']])
        for target in rt.snapshot(owner, run)['tasks']:
            detail = rt.task(owner, target['task_id'])
            for slot in detail['inputs']:
                if not slot['version_id'] or slot['receipt'] == 'accepted':
                    continue
                receipt_count += 1
                rt.accept_input(owner, target['task_id'], slot['port_id'], slot['version_id'], revision=target['revision'], accepted=True,
                                reason='Explicit per-consumer compatibility check', control_epoch=1, command_id='receipt-' + str(receipt_count))
                if target['task_key'] == 'reduce':
                    joined = rt.task(owner, target['task_id'])
                    if any(s['receipt'] != 'accepted' for s in joined['inputs']):
                        assert joined['state'] == 'waiting_inputs'
                        partial_join_checked = True
    assert partial_join_checked and asyncio.run(executor.run_once()) == []
    snapshot = rt.snapshot(owner, run)
    assert snapshot['budget']['used_tokens'] == 42 * len(raw['tasks'])
    rt.finalize(owner, run, expected_version=snapshot['run']['state_version'], control_epoch=1,
                command_id='final', evidence='Every current branch and required reducer input independently inspected')
    contexts = [json.loads(request.prompt.split('\n', 1)[1]) for request in invoker.requests]
    assert len(contexts[-1]['inputs']) == 2
    assert all('handoffs' not in context and 'messages' not in context for context in contexts)
    host.close()
    assert host.snapshot(run)['run']['state'] == 'completed'
