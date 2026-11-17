"""The saved OCR plan must preserve startup inputs and its real dependencies."""
import asyncio

import pytest

from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.oxr_books_template import BOOKS, oxr_books
from mewcode.orchestration.run_inputs import resolve_run_inputs
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.orchestration.templates import get_template, list_templates
from mewcode.orchestration.validate import validate_spec
from mewcode.workflow_core.migration import graph_from_flow
from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.state import fresh


def plan():
    return OrchestrationSpec.from_value(oxr_books(provider_key='test-provider'))


def test_template_survives_save_conversion_without_implicit_reviews():
    assert 'oxr_books' in {row['template_id'] for row in list_templates()}
    assert len(get_template('oxr_books').nodes) == 16
    spec = OrchestrationSpec.from_value(plan().as_dict())
    validate_spec(spec, require_bindings=True)
    flow = to_expert_flow(spec)
    assert flow.meta['run_input_contract'] == list(spec.goal_contract.run_inputs)
    graph = graph_from_flow(flow, goal=spec.goal)
    assert all(node['kind'] == 'work' for node in graph['nodes'])
    assert all(node['model'] == dict(provider_key='test-provider', model_id='gpt-6.1-sol', effort='max')
               for node in graph['nodes'])
    assert sum(book[3] for book in BOOKS) == 1073
    assert 'printed_page' in graph['goal'] and 'model_generated' in graph['goal']


def test_server_details_are_runtime_values_and_key_file_is_conditional():
    declarations = plan().goal_contract.run_inputs
    with pytest.raises(ValueError, match='ssh_host'):
        resolve_run_inputs(declarations, {})
    values = resolve_run_inputs(declarations, dict(ssh_host='ocr.example.test', ssh_user='research'))
    assert values['ssh_port'] == '22'
    assert values['ssh_auth'] == 'agent'
    assert not values.get('ssh_key_path')
    with pytest.raises(ValueError, match='ssh_key_path'):
        resolve_run_inputs(declarations, dict(ssh_host='ocr.example.test', ssh_user='research', ssh_auth='key_file'))
    values = resolve_run_inputs(declarations, dict(ssh_host='ocr.example.test', ssh_user='research',
        ssh_auth='key_file', ssh_key_path='C:/keys/ocr_ed25519', assets_base_url='https://assets.example.test/books/'))
    assert values['ssh_key_path'] == 'C:/keys/ocr_ed25519'
    assert values['assets_base_url'].startswith('https://')
    assert not any('password' in declaration['name'] for declaration in declarations)


def test_core_waits_for_environment_and_materials_then_joins_every_book():
    async def run():
        spec = plan()
        graph = graph_from_flow(to_expert_flow(spec), goal=spec.goal)
        state = fresh(graph)
        state['inputs'] = resolve_run_inputs(spec.goal_contract.run_inputs,
            dict(ssh_host='ocr.example.test', ssh_user='research'))
        backend = MemoryBackend(state)
        finished, entered, first_pair = set(), set(), asyncio.Event()

        async def execute(ctx):
            key = ctx.node_id
            assert ctx.inputs['ssh_host'] == 'ocr.example.test'
            if key in {'deploy', 'prepare'}:
                assert 'connect' in finished
                entered.add(key)
                if entered == {'deploy', 'prepare'}:
                    first_pair.set()
                await asyncio.wait_for(first_pair.wait(), timeout=2)
            if key.startswith('ocr_'):
                assert {'deploy', 'prepare'} <= finished
                assert {'deploy', 'prepare'} <= ctx.inputs.keys()
            if key.startswith(('questions_', 'knowledge_')):
                assert 'ocr_' + key.split('_', 1)[1] in finished
            if key == 'deliver':
                assert {f'{stage}_{book[0]}' for book in BOOKS
                        for stage in ('questions', 'knowledge')} <= finished
            finished.add(key)
            return dict(text=f'{key}: fake delivery for scheduler verification')

        await asyncio.wait_for(Core(backend, execute, concurrency=4).run(), timeout=5)
        assert backend.read()['status'] == 'completed'
        assert finished == {node['id'] for node in graph['nodes']}

    asyncio.run(run())
