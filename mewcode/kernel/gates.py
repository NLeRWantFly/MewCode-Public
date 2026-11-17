"""Five host-classified gates; only reversible choices have a deadline."""
import copy
from enum import StrEnum
from pathlib import Path
import time


class GateKind(StrEnum):
    PLAN = 'plan_approval'
    AUTHORIZATION = 'authorization_extension'
    IRREVERSIBLE = 'irreversible'
    BUSINESS = 'business_choice'
    EXHAUSTED = 'escalation_exhausted'
    REQUIRED = 'required_user_decision'
    CONSTRAINT = 'user_constraint_change'


def classify(record, node_id, *, kind='', request=None):
    if getattr(request, 'host_file_access', None):
        return GateKind.AUTHORIZATION
    grouped = getattr(request, 'group_members', ())
    if grouped:
        kinds = [classify(record, key, kind=kind, request=item) for key, item in grouped]
        return GateKind.AUTHORIZATION if GateKind.AUTHORIZATION in kinds else (
            GateKind.IRREVERSIBLE if GateKind.IRREVERSIBLE in kinds else
            GateKind.CONSTRAINT if GateKind.CONSTRAINT in kinds else GateKind.REQUIRED if GateKind.REQUIRED in kinds else kinds[0])
    node_id = node_id.split('::', 1)[0]
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.kernel.blockers import canonical_blocker, policy
    view = ledger_for_run(copy.deepcopy(record)).view(node_id.split('::', 1)[0])
    blockers = {canonical_blocker(fact.payload['kind']) for fact in view.active_blockers}
    if any(str(item) == 'unknown_effect' for item in blockers):
        return GateKind.IRREVERSIBLE
    if kind == 'permission' or any(policy(item).category == 'authority' for item in blockers):
        return GateKind.AUTHORIZATION
    grants = getattr(request, 'host_authorization_requests', ())
    if grants:
        from mewcode.experts.authorization_ledger import covers
        if any(covers(record, grant).status != 'covered' for grant in grants):
            return GateKind.AUTHORIZATION
    cases = record.get('recovery_cases') or {}
    if any(row.get('node_id') == node_id and row.get('status') == 'awaiting_user'
           and row.get('pending', {}).get('kind') == 'elevation' for row in cases.values()):
        return GateKind.AUTHORIZATION
    scope = (record.get('authorization_manifest') or {}).get('nodes', {}).get(node_id) or {}
    # A network capability does not make a parameter/plan choice irreversible.
    # Concrete external effects and permission extensions keep their gates.
    if scope.get('external_targets'):
        return GateKind.IRREVERSIBLE
    workspace = (record.get('host_execution_scope') or {}).get('workspace') or (
        record.get('authorization_manifest') or {}).get('workspace')
    if workspace:
        root = Path(workspace).resolve()
        for path in scope.get('write_paths', []):
            target = Path(path)
            if not (target if target.is_absolute() else root / target).resolve().is_relative_to(root):
                return GateKind.IRREVERSIBLE
    tool = getattr(request, 'tool_name', '')
    if record.get('orchestration_mode') == 'collaborative':
        # Models cannot label a missing business input as a reversible default.
        # Only a host-registered, reviewed optional question has that route.
        if getattr(request, 'collaboration_category', '') == 'constraint':
            return GateKind.CONSTRAINT
        optional = getattr(request, 'collaboration_optional_id', '')
        proof = (record.get('collaboration') or {}).get('optional_defaults', {}).get(optional)
        if not proof or proof.get('reviewed') is not True:
            return GateKind.REQUIRED
    if tool in {'IterationRecovery', 'SchedulerRecovery', 'KernelEscalation'}:
        return GateKind.EXHAUSTED
    if tool == 'human_oncall':
        return GateKind.PLAN
    return GateKind.BUSINESS


def recommended(request):
    if getattr(request, 'host_file_access', None):
        return None
    questions = getattr(request, 'questions', [])
    answer = {}
    for index, question in enumerate(questions):
        if not isinstance(question, dict) or question.get('input_mode', question.get('type')) == 'text':
            return None
        options = question.get('options') or []
        if not 2 <= len(options) <= 4:
            return None
        chosen = [row for row in options if isinstance(row, dict) and row.get('recommended')]
        if not chosen:
            chosen = [options[0]] if isinstance(options[0], dict) else []
        if len(chosen) != 1:
            return None
        value = chosen[0].get('value', chosen[0].get('id', chosen[0].get('label')))
        prepared = next((row for row in question.get('prepared_choices', []) if row.get('id') == value), {})
        if prepared.get('requires_input') or prepared.get('action') in {'manual', 'defer', 'clarify'}:
            return None
        if value is None:
            return None
        answer[question.get('name') or ('choice' if getattr(request, 'tool_name', '') == 'human_oncall' else 'q' + str(index))] = value
    return answer or None


def scope_identity(record, node_id, request, scope_hash=''):
    from mewcode.kernel.events import digest
    from mewcode.experts.waits import _question_material
    from mewcode.experts.node_ledger import ledger_for_run
    view = ledger_for_run(copy.deepcopy(record)).view(node_id.split('::', 1)[0])
    current = record.get('node_states', {}).get(node_id) or record.get('members', {}).get(node_id) or {}
    collaboration_identity = None
    if record.get('orchestration_mode') == 'collaborative':
        from mewcode.orchestration.ask_gateway import identity
        collaboration_identity = identity(record, node_id, request.questions)
    return digest([node_id, view.epoch, current.get('input_revision'), scope_hash,
        (record.get('authorization_manifest') or {}).get('scope_digest'), _question_material(request.questions),
        *([request.host_file_access] if getattr(request, 'host_file_access', None) else []),
        *([collaboration_identity] if collaboration_identity is not None else [])])


def prepare(record, node_id, request, *, kind, scope_hash='', timeout=120):
    identity = scope_identity(record, node_id, request, scope_hash)
    gate_kind = classify(record, node_id, kind=kind, request=request)
    reversible = gate_kind in {GateKind.PLAN, GateKind.BUSINESS, GateKind.EXHAUSTED}
    gates = record.setdefault('kernel_gates', {})
    existing = gates.get(request.request_id)
    if existing and existing['scope_identity'] == identity:
        return existing
    gate = {'gate_id': request.request_id, 'node_id': node_id, 'kind': gate_kind.value,
            'scope_identity': identity, 'reversible': reversible, 'created_at': time.time(),
            'deadline': time.time() + timeout if reversible else None,
            'recommended': recommended(request), 'status': 'waiting',
            'timeout_consequence': '先保存快照，再按推荐项继续。快照失败则等待。' if reversible else '持续等待你的明确选择。'}
    gates[request.request_id] = gate
    return gate


async def snapshot_before_timeout(store, run_id, node_id, *, manager_factory=None, request=None):
    from mewcode.kernel.snapshots import SnapshotManager
    record = store.load(run_id) or {}
    workspace = (record.get('host_execution_scope') or {}).get('workspace') or (
        record.get('authorization_manifest') or {}).get('workspace')
    if not workspace:
        raise ValueError('没有可核验的工作区，不能在超时后自动继续。')
    manager = (manager_factory or SnapshotManager)(workspace)
    snapshot = await manager.create(run_id=run_id, node_id=node_id,
        reason='before_timeout_recommended_choice', event_sequence=record.get('kernel_revision', 0))
    members = getattr(request, 'group_members', ()) or [(node_id, request)]
    scopes = [(record.get('authorization_manifest') or {}).get('nodes', {}).get(key) or {} for key, _ in members]
    scope = {'write_paths': [path for item in scopes for path in item.get('write_paths', [])]}
    if not manager.covers(snapshot['snapshot_id'], scope.get('write_paths', [])):
        raise ValueError('本次写入范围未被快照覆盖，持续等待你的选择。')
    for path, row in snapshot.get('files', {}).items():
        target = (Path(workspace) / path).resolve()
        if not row['covered'] and any(target.is_relative_to(
            (Path(root) if Path(root).is_absolute() else Path(workspace) / root).resolve())
            for root in scope.get('write_paths', [])):
            raise ValueError('写入范围包含未覆盖的大文件，不能自动继续。')
    return snapshot


def group_display(entries):
    """Combine prepared questions without losing each source's answer scope."""
    from mewcode.kernel.events import digest
    first_node, first = entries[0]
    display = copy.copy(first)
    display.questions, prepared, mapping = [], [], {}
    collaboration_display = []
    for number, (node_id, item) in enumerate(entries):
        shells = {row['name']: row for row in item.choice_bundle.get('questions', [])}
        for index, question in enumerate(item.questions):
            name = question.get('name') or 'q' + str(index)
            wire_name = f'g{number}_{name}'
            mapping[wire_name] = (number, name)
            display.questions.append({**copy.deepcopy(question), 'name': wire_name,
                'question': f'{node_id}：' + str(question.get('question') or question.get('message') or '')})
            if getattr(item, 'collaboration_display_questions', None):
                collaboration_display.append({**copy.deepcopy(item.collaboration_display_questions[index]), 'name': wire_name})
            if name in shells:
                prepared.append({**copy.deepcopy(shells[name]), 'name': wire_name})
    if collaboration_display:
        display.collaboration_display_questions = collaboration_display
        gates = [getattr(item, 'host_gate', {}) for _, item in entries]
        strict = next((gate for gate in gates if gate.get('reversible') is False), None)
        if strict:
            display.host_gate = {**strict, 'deadline': None, 'reversible': False}
    if all(item.choice_bundle for _, item in entries):
        identity = digest([item.choice_bundle for _, item in entries])
        display.choice_bundle = {'id': identity, 'version': identity, 'status': 'ready', 'questions': prepared}
    else:
        display.choice_bundle = {}
    display.group_members, display.group_mapping = entries, mapping
    display.host_gate = copy.deepcopy(first.host_gate)
    if any(not item.host_gate['reversible'] for _, item in entries):
        display.host_gate.update(reversible=False, deadline=None, kind='irreversible',
            timeout_consequence='持续等待你的明确选择。')
    display.choice_resolution = 'wait'
    def signature():
        return digest([item.choice_state_signature() if callable(getattr(item, 'choice_state_signature', None))
                       else item.host_gate['scope_identity'] for _, item in entries])
    display.choice_state_signature = signature
    display.choice_display_signature = signature()
    display.scope_current = lambda: signature() == display.choice_display_signature
    return display


def distribute_group(display, outcome):
    from mewcode.experts.waits import _WaitOutcome
    answer = outcome.value if isinstance(outcome, _WaitOutcome) else outcome
    if not isinstance(answer, dict):
        for _, item in display.group_members[1:]:
            if not item.future.done():
                item.future.set_result(answer)
        return outcome
    answers = [{} for _ in display.group_members]
    for wire, (number, name) in display.group_mapping.items():
        if wire in answer:
            answers[number][name] = answer[wire]
    for number, (_, item) in enumerate(display.group_members):
        item.answer_notes = {name: display.answer_notes.get(wire, '') for wire, (owner, name) in display.group_mapping.items()
                             if owner == number and display.answer_notes.get(wire)}
        item.choice_inputs = copy.deepcopy(display.choice_inputs)
        if number and not item.future.done():
            item.group_answered = True
            item.future.set_result(_WaitOutcome(answers[number], mode=outcome.mode) if isinstance(outcome, _WaitOutcome) else answers[number])
    return _WaitOutcome(answers[0], mode=outcome.mode) if isinstance(outcome, _WaitOutcome) else answers[0]
