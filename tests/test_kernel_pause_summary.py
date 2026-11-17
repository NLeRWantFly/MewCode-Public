from mewcode.experts.pause_summary import pause_reason, pause_summary


def test_pause_summary_explains_current_blocker_and_never_invents_missing_fields():
    record = {'node_states': {'N03': {'status': 'paused', 'error_kind': 'capability_missing'}}}
    row = pause_summary(record)[0]
    assert row['node_id'] == 'N03' and row['blocker'] == 'capability_missing'
    assert row['owner'] == row['next'] == '未知'
    assert '处理人：未知' in pause_reason(record)


def test_completed_nodes_and_empty_checkpoint_keep_original_reason():
    assert pause_reason({'node_states': {}}, '用户暂停') == '用户暂停'
    assert not pause_summary({'node_states': {'N03': {'status': 'completed', 'error_kind': 'capability_missing'}}})
