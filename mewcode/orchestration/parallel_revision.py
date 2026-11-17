"""A stopped, explicitly requested fork/join revision with durable provenance.

This host operation preserves original deliveries and budgets. Changed contracts
are rechecked; no acceptance result is manufactured by a graph migration.
"""
from __future__ import annotations

import copy

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.collaboration_recovery import unresolved_calls
from mewcode.orchestration.collab_compile import compile_plan, validate_contracts
from mewcode.orchestration.loops import LoopContract, compile_loops
from mewcode.orchestration.parallel import flow_groups


def adopted_plan_hash(record, plan_hash):
    """Accept only the exact latest host-adopted design in this run."""
    value = record.get('collaboration') or {}
    rows = value.get('parallel_design_revisions') or []
    if not rows:
        return False
    row = rows[-1]
    source = value.get('user_sources', {}).get(row.get('source_ref')) or {}
    return (row.get('plan_hash') == plan_hash and row.get('flow_hash') == ledger.digest(record['locked_flow'])
            and source.get('role') == 'user' and source.get('text') == row.get('request')
            and row.get('status') == 'adopted')


def revise_parallel_flow(snapshot, flow, *, source_ref, request):
    """Return a candidate; caller must archive and CAS-commit the stopped run."""
    if (snapshot.get('status') not in {'paused', 'blocked', 'needs_attention'}
            or snapshot.get('active_node_ids')
            or unresolved_calls(snapshot, (snapshot.get('collaboration') or {}).get('contracts', {}))):
        raise ValueError('并发修订必须等待原运行暂停且在途操作全部核对')
    if not isinstance(source_ref, str) or not source_ref or not isinstance(request, str) or not request.strip():
        raise ValueError('并发修订必须保留用户的明确要求和来源')
    old = ExpertFlowSpec.from_value(snapshot['locked_flow'])
    new = ExpertFlowSpec.from_value(flow)
    value = ledger.state(snapshot)
    if (snapshot.get('pending_recovery_revision')
            or any(p.get('status') in {'proposed', 'needs_attention'} for p in value.get('proposals', {}).values())
            or any(c.get('held') for c in value['contracts'].values())):
        raise ValueError('已有运行图扩展或待处理要求修订，不能覆盖')
    if (new.flow_id != old.flow_id or new.finalizer_node_id != old.finalizer_node_id
            or new.finalizer_plugin != old.finalizer_plugin or new.node_map.keys() != old.node_map.keys()):
        raise ValueError('并发修订不能改变运行身份、节点集合或最终关口')
    old_loops = {x['loop_id']: x for x in old.meta.get('loops', [])}
    new_loops = {x['loop_id']: x for x in new.meta.get('loops', [])}
    if old_loops.keys() != new_loops.keys():
        raise ValueError('并发修订不能替换循环集合')
    compile_loops(new.nodes, new.edges, new.meta.get('loops'))
    groups = flow_groups(new)
    if not groups or flow_groups(old):
        raise ValueError('此迁移仅将现有串行分支首次改为声明的并发组')
    branch_ids = {key for group in groups for key in group.node_ids}
    join_ids = {group.join_node_id for group in groups}
    changed_loops = {key for key in old_loops if old_loops[key] != new_loops[key]}
    content_ids = set(join_ids)
    for key in changed_loops:
        a, b = old_loops[key], new_loops[key]
        editable = {'parallel_groups', 'criteria', 'exit_condition'}
        if {k:v for k,v in a.items() if k not in editable} != {k:v for k,v in b.items() if k not in editable}:
            raise ValueError('并发迁移必须保留循环范围、审查者、返修根和所有资源限额')
        content_ids.update(a.get('reviewer_node_ids', []))
        content_ids.update(a.get('required_node_ids', []))
        content_ids.add(a['check_node_id'])
    allowed_meta = {'loops', 'node_plan_briefs', 'pipeline_graph_version', 'pipeline_graph_updated_at'}
    if {k:v for k,v in old.meta.items() if k not in allowed_meta} != {k:v for k,v in new.meta.items() if k not in allowed_meta}:
        raise ValueError('并发迁移不能修改其他规格、授权或输入参数')
    for key in old.node_map:
        a, b = old.node_map[key].as_dict(), new.node_map[key].as_dict()
        allowed = {'position'} | ({'responsibility', 'label'} if key in content_ids else set())
        if {k:v for k,v in a.items() if k not in allowed} != {k:v for k,v in b.items() if k not in allowed}:
            raise ValueError('并发迁移不能修改独立识别方法、模型或节点能力：' + key)
        if key not in content_ids and (old.meta.get('node_plan_briefs', {}).get(key) != new.meta.get('node_plan_briefs', {}).get(key)):
            raise ValueError('并发迁移不能改写独立分支或其他阶段的工作要求：' + key)
    a_edges, b_edges = [e.as_dict() for e in old.edges], [e.as_dict() for e in new.edges]
    if any(e['to_node'] not in branch_ids | join_ids for e in a_edges + b_edges if e not in a_edges or e not in b_edges):
        raise ValueError('并发迁移只能修改分支和汇合的输入连线')
    from mewcode.orchestration.convert import from_expert_flow
    from mewcode.orchestration.maintenance import _preserve_gates
    _preserve_gates(from_expert_flow(old), from_expert_flow(new))
    before, after = compile_plan(old), compile_plan(new)
    changed = {key for key in before['contracts'] if before['contracts'][key] != after['contracts'][key]}
    if not changed or changed - (branch_ids | content_ids):
        raise ValueError('并发修订影响了范围外合同')
    runtime = None
    if value.get('runtime_flow'):
        from mewcode.orchestration.revisions import _published_execution_flow
        published = _published_execution_flow(snapshot)
        published_raw = published.as_dict()
        old_raw = old.as_dict()
        if (published.meta != old.meta or published.finalizer_node_id != old.finalizer_node_id
                or published.finalizer_plugin != old.finalizer_plugin
                or any(key not in published.node_map or node.as_dict() != published.node_map[key].as_dict()
                       for key,node in old.node_map.items())
                or any(edge not in published_raw['edges'] for edge in old_raw['edges'])):
            raise ValueError('已发布运行图修改了原流程，不能覆盖')
        extra_edges = [edge for edge in published_raw['edges'] if edge not in old_raw['edges']]
        if any({edge['from_node'],edge['to_node']} & changed for edge in extra_edges):
            raise ValueError('已发布节点与并发修改范围重叠，不能覆盖')
        runtime = copy.deepcopy(new.as_dict())
        runtime['version'] = max(published.version + 1, new.version)
        runtime['nodes'] += [copy.deepcopy(node) for node in published_raw['nodes'] if node['node_id'] not in old.node_map]
        runtime['edges'] += copy.deepcopy(extra_edges)
        runtime = ExpertFlowSpec.from_value(runtime).as_dict()
    candidate = copy.deepcopy(snapshot)
    current = ledger.state(candidate)
    if source_ref in current.get('user_sources', {}):
        raise ValueError('此用户来源已有修订，不能复用身份提交另一版本')
    current.setdefault('user_sources', {})[source_ref] = {'role':'user', 'text':request.strip(), 'kind':'parallel_flow_revision'}
    rechecks = {}
    for key in changed:
        if key in branch_ids and ledger.valid_certificate(current, key):
            cert = copy.deepcopy(current['certificates'][key])
            prior = current['attempts'][cert['attempt_id']]
            if (prior.get('delivery') or {}).get('status') == 'completed' and not prior['delivery'].get('error_kind'):
                rechecks[key] = {'source_attempt_id':prior['id'], 'delivery_hash':ledger.digest(prior['delivery']),
                    'artifacts':cert['artifacts'], 'source_ref':source_ref}
    # Compute affected contracts on both graphs; old evidence remains historical.
    affected = ledger.invalidate(current, changed, '用户将独立识别改为并发后重新核验相关交接')
    for key in changed:
        prior, proposed, contract = before['contracts'][key], after['contracts'][key], current['contracts'][key]
        for field in prior.keys() | proposed.keys():
            if prior.get(field) == proposed.get(field):
                continue
            if contract.get(field) != prior.get(field):
                raise ValueError('合同已有其他有效修订，未覆盖：' + key + '/' + field)
            if field in proposed:
                contract[field] = copy.deepcopy(proposed[field])
            else:
                contract.pop(field, None)
        contract['version'] += 1
        contract.pop('bound_dependencies', None)
        if key in content_ids:
            contract['goal'] += '\n最新用户要求（' + source_ref + '）：' + request.strip()
    validate_contracts(current['contracts'], current['entries'],
                       {n['node_id'] for n in runtime['nodes']} if runtime else set(new.node_map))
    current.setdefault('parallel_delivery_rechecks', {}).update(rechecks)
    for loop_id in changed_loops:
        loop = LoopContract.from_value(new_loops[loop_id])
        state = candidate.get('loop_states', {}).get(loop_id)
        if not state:
            continue
        if state.get('status') == 'completed' or any(key.startswith(loop_id + '/') for key in candidate['loop_states']):
            raise ValueError('已结束或嵌套循环需要独立迁移，未覆盖历史进度')
        from mewcode.experts.loop_recovery import invalidate_results
        invalidate_results(new, loop, state, sorted(changed & set(loop.node_ids)), reason='user_parallel_revision')
        state.update(contract_hash=loop.contract_hash, contract_snapshot=loop.as_dict(), status='running',
                     round_open=True, reason='', error_kind='', failed_node_id='', active_node='', active_nodes=[], active_at=None)
        state.pop('carry_results', None)
        for field in ('loop_control_policy', 'loop_resource_policy'):
            if state.get(field):
                state[field]['contract_hash'] = loop.contract_hash
        mirror = candidate.get('flow_state') or {}
        if mirror.get('loop_state_key') == loop_id:
            mirror.update(loop_state=copy.deepcopy(state), loop_contract=loop.as_dict(), node_id='')
        # Existing admitted work, counters and item cursors are retained.
        parent_ids = {e.from_node for e in new.edges if e.to_node == loop.entry_node_id and e.from_node not in loop.node_ids}
        current.setdefault('loop_bases', {})[loop_id] = ledger.digest({
            'contracts':{key:ledger.semantic_basis(current,key) for key in loop.node_ids},
            'inputs':{key:current['certificates'].get(key) for key in sorted(parent_ids)}})
    for key in affected:
        for collection in ('node_states', 'stages'):
            state = candidate.get(collection, {}).get(key)
            if state:
                state.update(status='pending', passed=False, status_reason='并发修订后待核验；已有产物和工具记录保留')
    current['plan_version'] += 1
    current['spec_version'] += 1
    revision = {'source_ref':source_ref,'request':request.strip(),'status':'adopted',
        'previous_flow_hash':ledger.digest(old.as_dict()),'flow_hash':ledger.digest(new.as_dict()),
        'previous_plan_hash':ledger.digest(before),'plan_hash':ledger.digest(after),
        'affected_node_ids':sorted(affected), 'recheck_node_ids':sorted(rechecks),
        'plan_version':current['plan_version']}
    if runtime:
        revision.update(previous_runtime_flow_hash=current['runtime_flow_hash'], runtime_flow_hash=ledger.digest(runtime))
        current.update(runtime_flow=runtime, runtime_flow_hash=revision['runtime_flow_hash'])
        current['flow_versions'].append({'plan_version':current['plan_version'], 'flow':copy.deepcopy(runtime),
            'flow_hash':revision['runtime_flow_hash'], 'user_parallel_revision':source_ref})
    current.setdefault('parallel_design_revisions', []).append(revision)
    ledger.event(current,'user_parallel_design_adopted',**revision)
    from mewcode.expert_runs import expert_flow_fingerprint
    from mewcode.orchestration.revisions import execution_fingerprint
    candidate.update(locked_flow=new.as_dict(), flow_version=new.version, flow_fingerprint=expert_flow_fingerprint(new),
        saved_plan_fingerprint=execution_fingerprint(new), status='paused', active_node_ids=[],
        status_reason='已采用用户要求的并发融合流程；保留已有成果，等待继续')
    return candidate


def validate_runtime_revision(snapshot, latest):
    """Verify an explicit user revision while retaining its earlier publication."""
    value = snapshot['collaboration']
    row = value['parallel_design_revisions'][-1]
    previous = value['flow_versions'][-2]
    publication = value['graph_publications'][previous['publication_id']]
    if (latest.get('user_parallel_revision') != row['source_ref']
            or not adopted_plan_hash(snapshot, row['plan_hash'])
            or latest['plan_version'] != row['plan_version']
            or latest['flow_hash'] != row.get('runtime_flow_hash')
            or latest['flow_hash'] != value['runtime_flow_hash']
            or ledger.digest(latest['flow']) != latest['flow_hash']
            or previous['flow_hash'] != row.get('previous_runtime_flow_hash')
            or ledger.digest(previous['flow']) != previous['flow_hash']
            or publication['status'] != 'published'
            or publication['new_plan_version'] != previous['plan_version']
            or ledger.digest(publication['fragment']['flow']) != previous['flow_hash']):
        raise ValueError('并发修订缺少对应的用户要求或原发布记录')


def saved_branch_delivery(value, node_id, acceptance):
    item = value.get('parallel_delivery_rechecks', {}).get(node_id)
    if not item:
        return None
    old = value['attempts'].get(item['source_attempt_id']) or {}
    delivery = old.get('delivery')
    if (not delivery or ledger.digest(delivery) != item['delivery_hash']
            or not acceptance.unchanged(item['artifacts'])):
        return None
    return copy.deepcopy(delivery)
