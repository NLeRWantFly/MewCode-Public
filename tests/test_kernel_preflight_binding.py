import copy

from mewcode.experts.authorization_ledger import record_authorization
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel.authorization_binding import bind_call, request_for_call
from mewcode.kernel.capabilities import compile_manifest, declaration
from mewcode.kernel.preflight import inspect
from mewcode.kernel.preflight_binding import prepare_manifest
from mewcode.kernel.repairs import extend_manifest
from mewcode.permissions.task_authorization import TaskAuthorization


def _flow(node, *, meta=None):
    return ExpertFlowSpec.from_value({
        'flow_id': 'preflight-binding-fixture',
        'finalizer_node_id': node['node_id'],
        'edges': [],
        'nodes': [node],
        'meta': meta or {},
    })


def _flow_nodes(*nodes):
    return ExpertFlowSpec.from_value({
        'flow_id': 'preflight-binding-multi-fixture',
        'finalizer_node_id': nodes[-1]['node_id'],
        'edges': [],
        'nodes': list(nodes),
    })


def _null_node(node_id='N01', **extra):
    return {'node_id': node_id, 'role': 'executor', **extra}


def test_task_authorization_is_saved_but_not_compiled_as_broad_capabilities(tmp_path):
    authorization = TaskAuthorization.from_user_scope({
        'write_directories': [str(tmp_path)],
        'network_hosts': ['example.test'],
        'allowed_tools': ['EditFile', 'WebFetch'],
    }, task_id='task-1')
    flow = _flow(_null_node(tools=['EditFile', 'WebFetch']))
    record = {}

    manifest = prepare_manifest(record, flow, tmp_path, authorization, task_id='task-1')

    assert record['authorization_manifest'] is manifest
    assert manifest['task_authorization'] == authorization.as_dict()
    assert manifest['task_id'] == 'task-1'
    assert manifest['nodes']['N01'] == declaration({})
    assert manifest['nodes']['N01']['write_paths'] == []
    assert manifest['nodes']['N01']['network_hosts'] == []


def test_task_authorization_requires_an_allowed_matching_tool_and_does_not_enable_commands(tmp_path):
    edit_only = TaskAuthorization.from_user_scope({
        'write_directories': [str(tmp_path)], 'allowed_tools': ['EditFile'],
    }, task_id='task-1')
    mismatch = prepare_manifest({}, _flow(_null_node(tools=['WriteFile'])), tmp_path,
                                edit_only, task_id='task-1')
    assert mismatch['nodes']['N01'] is None

    read_only_node = prepare_manifest({}, _flow(_null_node(tools=['ReadFile'])), tmp_path,
                                      edit_only, task_id='task-1')
    assert read_only_node['nodes']['N01'] == declaration({})

    command_behavior = TaskAuthorization.from_user_scope({
        'allowed_behaviors': ['isolated_command'], 'allowed_tools': [],
    }, task_id='task-1')
    command_flow = _flow(_null_node(tools=['Bash']))
    command_manifest = prepare_manifest({}, command_flow, tmp_path, command_behavior, task_id='task-1')
    assert command_manifest['nodes']['N01'] is None

    edit_with_wildcard = prepare_manifest({}, _flow(_null_node(tools=['*'], deny_tools=['WriteFile'])),
        tmp_path, edit_only, task_id='task-1')
    assert edit_with_wildcard['nodes']['N01'] == declaration({})

    denied_write = TaskAuthorization.from_user_scope({
        'write_directories': [str(tmp_path)], 'allowed_tools': ['WriteFile'],
    }, task_id='task-1')
    wildcard_denied = prepare_manifest({}, _flow(_null_node(tools=['*'], deny_tools=['WriteFile'])),
        tmp_path, denied_write, task_id='task-1')
    assert wildcard_denied['nodes']['N01'] is None


def test_only_explicit_gate_or_read_only_nodes_are_auto_declared_without_authority(tmp_path):
    flow = ExpertFlowSpec.from_value({
        'flow_id': 'read-only-fixture', 'finalizer_node_id': 'N04', 'edges': [],
        'meta': {'node_kinds': {'N01': 'gate'}},
        'nodes': [_null_node('N01'), _null_node('N02', tools=['ReadFile', 'Glob', 'Grep']),
                  _null_node('N03', tools=[]), _null_node('N04')],
    })
    manifest = prepare_manifest({}, flow, tmp_path)

    assert manifest['nodes']['N01'] == declaration({})
    assert manifest['nodes']['N02'] == declaration({})
    assert manifest['nodes']['N03'] == declaration({})
    assert manifest['nodes']['N04'] is None
    findings = inspect(manifest)
    assert [(row['code'], row['entry_ids']) for row in findings if row['severity'] == 'error'] == [
        ('undeclared_capabilities', ['N04'])]


def test_task_authorization_must_match_current_task_and_be_live(tmp_path):
    authorization = TaskAuthorization.from_user_scope({
        'write_directories': [str(tmp_path)], 'allowed_tools': ['WriteFile'],
    }, task_id='task-1')
    mismatch = prepare_manifest({}, _flow(_null_node()), tmp_path, authorization, task_id='task-2')
    assert mismatch['nodes']['N01'] is None
    assert 'task_authorization' not in mismatch
    assert 'task_id' not in mismatch

    expired = authorization.as_dict()
    expired['expires_at'] = 1
    stale = prepare_manifest({}, _flow(_null_node()), tmp_path, expired, task_id='task-1')
    assert stale['nodes']['N01'] is None
    assert 'task_authorization' not in stale


def test_broad_live_ledger_grant_only_initializes_an_empty_scope(tmp_path):
    record = {}
    record_authorization(record, kind='typed_grant', scope={
        'operation_classes': ['write_outside_workspace'], 'paths': [str(tmp_path)],
    }, quote='fixture grant', source_ref='grant-fixture', host_verified=True)

    manifest = prepare_manifest(record, _flow(_null_node()), tmp_path)

    assert manifest['nodes']['N01'] == declaration({})
    assert not manifest['nodes']['N01']['write_paths']
    assert record['authorizations']['entries'][0]['scope']['paths'] == [str(tmp_path)]


def test_tool_scoped_ledger_grant_matches_node_tools_without_widening_tools(tmp_path):
    record = {}
    record_authorization(record, kind='typed_grant', scope={
        'operation_classes': ['write_outside_workspace'], 'paths': [str(tmp_path)],
        'tools': ['WriteFile'],
    }, quote='fixture WriteFile grant', source_ref='writefile-grant', host_verified=True)

    matching = prepare_manifest(copy.deepcopy(record), _flow(_null_node(tools=['WriteFile'])), tmp_path)
    mismatched = prepare_manifest(copy.deepcopy(record), _flow(_null_node(tools=['EditFile'])), tmp_path)

    assert matching['nodes']['N01'] == declaration({})
    assert mismatched['nodes']['N01'] is None
    assert record['authorizations']['entries'][0]['scope']['tools'] == ['WriteFile']


def test_ledger_grants_keep_node_and_recovery_identity_limits(tmp_path):
    base = {'operation_classes': ['write_outside_workspace'], 'paths': [str(tmp_path)]}
    restricted = [
        {**base, 'node_ids': ['N99']},
        {**base, 'epoch': 1},
        {**base, 'case_id': 'case-1'},
        {**base, 'round': 1},
        {**base, 'operation_digest': 'exact-call-only'},
    ]
    for index, scope in enumerate(restricted):
        record = {}
        record_authorization(record, kind='gate_answer', scope=scope,
            quote='fixture grant', source_ref=f'limited-{index}', host_verified=True)
        manifest = prepare_manifest(record, _flow(_null_node()), tmp_path)
        assert manifest['nodes']['N01'] is None


def test_page_image_authority_adds_only_the_explicit_route_to_its_node(tmp_path):
    record = {}
    record_authorization(record, kind='gate_answer', scope={
        'operation_classes': ['external_send'], 'model_routes': ['provider/vision'],
        'content_classes': ['page_image'], 'node_ids': ['N01'],
        'tools': ['InspectVisualEvidence'], 'cost_cap_tokens': 40000,
    }, quote='fixture visual grant', source_ref='visual-parent', host_verified=True)
    flow = _flow_nodes(_null_node('N01', tools=['InspectVisualEvidence']),
                       _null_node('N01::child', tools=['InspectVisualEvidence']))

    manifest = prepare_manifest(record, flow, tmp_path)

    assert manifest['nodes']['N01'] == declaration({'vision_routes': ['provider/vision']})
    assert manifest['nodes']['N01::child'] is None


def test_page_image_authority_needs_a_live_grant_and_inspection_tool_scope(tmp_path):
    flow = _flow(_null_node(tools=['InspectVisualEvidence']))
    assert prepare_manifest({}, flow, tmp_path)['nodes']['N01'] is None

    record = {}
    record_authorization(record, kind='gate_answer', scope={
        'operation_classes': ['external_send'], 'model_routes': ['provider/vision'],
        'content_classes': ['page_image'], 'tools': ['ReadFile'],
    }, quote='wrong tool', source_ref='visual-wrong-tool', host_verified=True)
    assert prepare_manifest(record, flow, tmp_path)['nodes']['N01'] is None


def test_page_image_denial_takes_precedence_over_a_matching_grant(tmp_path):
    record = {}
    scope = {'operation_classes': ['external_send'], 'model_routes': ['provider/vision'],
             'content_classes': ['page_image'], 'node_ids': ['N01']}
    record_authorization(record, kind='gate_answer', scope=scope, quote='allow',
                         source_ref='visual-allow', host_verified=True)
    record_authorization(record, kind='denial', scope=scope, quote='deny',
                         source_ref='visual-deny')

    manifest = prepare_manifest(record, _flow(_null_node(tools=['InspectVisualEvidence'])), tmp_path)

    assert manifest['nodes']['N01'] is None


def test_page_image_authority_does_not_drop_unavailable_episode_identity(tmp_path):
    base = {'operation_classes': ['external_send'], 'model_routes': ['provider/vision'],
            'content_classes': ['page_image'], 'node_ids': ['N01']}
    restricted = [
        {**base, 'epoch': 1},
        {**base, 'case_id': 'case-1'},
        {**base, 'round': 1},
        {**base, 'operation_digest': 'exact-visual-call'},
    ]
    for index, scope in enumerate(restricted):
        record = {}
        record_authorization(record, kind='gate_answer', scope=scope,
            quote='fixture visual grant', source_ref=f'visual-limited-{index}', host_verified=True)
        manifest = prepare_manifest(record, _flow(_null_node(tools=['InspectVisualEvidence'])), tmp_path)
        assert manifest['nodes']['N01'] is None


def test_valid_exact_call_binding_survives_recompilation_for_same_workspace_and_node(tmp_path):
    flow = _flow(_null_node())
    record = {'authorization_manifest': compile_manifest(flow, workspace=str(tmp_path), approved=True)}
    arguments = {'file_path': 'output.txt', 'content': 'fixture'}
    request = request_for_call('N01', 'WriteFile', arguments, str(tmp_path))
    record_authorization(record, kind='gate_answer', scope={
        **request['scope'], 'operation_classes': [request['operation_class']],
    }, quote='fixture exact call', source_ref='exact-call-fixture', host_verified=True)
    assert bind_call(record, 'N01', 'WriteFile', arguments)

    manifest = prepare_manifest(record, flow, tmp_path)

    assert manifest['nodes']['N01'] == declaration({})
    assert len(manifest['call_bindings']) == 1
    binding = next(iter(manifest['call_bindings'].values()))
    assert binding['node_id'] == 'N01'
    assert binding['request'] == request

    different_node_flow = _flow(_null_node('N02'))
    different_node = prepare_manifest(copy.deepcopy(record), different_node_flow, tmp_path)
    assert 'call_bindings' not in different_node
    assert different_node['nodes']['N02'] is None

    different_workspace = tmp_path / 'other'
    different_workspace.mkdir()
    moved = prepare_manifest(copy.deepcopy(record), flow, different_workspace)
    assert 'call_bindings' not in moved
    assert moved['nodes']['N01'] is None


def test_manual_extension_requires_live_receipt_same_workspace_and_compatible_graph(tmp_path):
    original_flow = _flow(_null_node(capabilities={'write_paths': ['outputs']}))
    record = {'node_states': {'N01': {}}, 'authorization_manifest': compile_manifest(
        original_flow, workspace=str(tmp_path), approved=True)}
    extend_manifest(record, node_id='N01', capabilities={'write_paths': ['outputs', 'reports']},
                    source_ref='extension-fixture')
    extended_record = copy.deepcopy(record)

    kept = prepare_manifest(copy.deepcopy(extended_record), original_flow, tmp_path)
    assert kept['nodes']['N01']['write_paths'] == ['outputs', 'reports']

    changed_flow = _flow(_null_node(capabilities={'write_paths': ['different']}))
    changed = prepare_manifest(copy.deepcopy(extended_record), changed_flow, tmp_path)
    assert changed['nodes']['N01']['write_paths'] == ['different']

    without_receipt = {'authorization_manifest': extended_record['authorization_manifest'],
                       'authorizations': {'version': 1, 'entries': []}}
    unverified = prepare_manifest(without_receipt, original_flow, tmp_path)
    assert unverified['nodes']['N01']['write_paths'] == ['outputs']

    denied_extension = copy.deepcopy(extended_record)
    receipt_scope = denied_extension['authorizations']['entries'][0]['scope']
    record_authorization(denied_extension, kind='denial', scope=receipt_scope,
        quote='deny extension', source_ref='deny-extension-fixture')
    denied = prepare_manifest(denied_extension, original_flow, tmp_path)
    assert denied['nodes']['N01']['write_paths'] == ['outputs']

    other_workspace = tmp_path / 'other'
    other_workspace.mkdir()
    moved = prepare_manifest(copy.deepcopy(extended_record), original_flow, other_workspace)
    assert moved['nodes']['N01']['write_paths'] == ['outputs']
