import pytest

from mewcode.teams.composition import ComponentLink, compose_team, map_team
from mewcode.teams.spec_compiler import SpecCompilationError
from test_team_spec_v2 import compile_context, compile_definition, graph_definition


def test_serial_composition_pins_and_flattens():
    child = compile_definition(graph_definition())
    plan = compose_team(spec_id='pipeline', version=1, components={'first': child, 'second': child},
                        inputs=child.spec.inputs, outputs=child.spec.outputs, policy=child.effective_policy,
                        context=compile_context(), links=(ComponentLink(None, 'request', 'first', 'request'),
                        ComponentLink('first', 'delivery', 'second', 'request'), ComponentLink('second', 'delivery', None, 'delivery')))
    assert len(plan.compiled.task_order) == 4
    assert plan.compiled.task_order.index('first__second') < plan.compiled.task_order.index('second__produce')
    assert plan.sources[0][2] == child.resolved_spec_hash
    assert sum(t.token_budget for t in plan.compiled.spec.tasks) == 400


def test_bounded_map_uses_one_dag():
    child = compile_definition()
    plan = map_team(child, ('a', 'b'), spec_id='mapped', policy=child.effective_policy, context=compile_context())
    assert set(plan.compiled.task_order) == {'a__produce', 'b__produce'}
    assert {p.port_id for p in plan.compiled.spec.inputs} == {'a__request', 'b__request'}
    with pytest.raises(SpecCompilationError, match='DUPLICATE_ITEM'):
        map_team(child, ('a', 'a'), spec_id='bad', policy=child.effective_policy, context=compile_context())
    with pytest.raises(SpecCompilationError, match='COMPOSITION_LIMIT'):
        map_team(child, tuple('item' + str(i) for i in range(17)), spec_id='bad', policy=child.effective_policy, context=compile_context())


def test_composition_budget_does_not_multiply_grant():
    child = compile_definition()
    with pytest.raises(SpecCompilationError, match='BUDGET_EXCEEDED'):
        map_team(child, tuple('item' + str(i) for i in range(11)), spec_id='bad', policy=child.effective_policy, context=compile_context())
