from __future__ import annotations

import pytest

from mewcode.teams.shared_task import SharedTaskStore, TaskConflictError


@pytest.fixture
def store(tmp_path):
    return SharedTaskStore(tmp_path / "tasks.json")


def test_create_and_get(store):
    t = store.create(title="t1", created_by="alice")
    assert store.get(t.id).title == "t1"


def test_create_reloads_from_disk_no_id_collision(tmp_path):
    """两个实例先后 create，不得复用 id、不得覆盖对方任务。"""
    path = tmp_path / "tasks.json"
    a = SharedTaskStore(path)
    b = SharedTaskStore(path)
    ta = a.create(title="from-a")
    tb = b.create(title="from-b")
    assert ta.id != tb.id
    ids = {t.id for t in a.list_tasks()}
    assert {ta.id, tb.id} <= ids


def test_create_unknown_dependency_rejected(store):
    with pytest.raises(TaskConflictError):
        store.create(title="t", blocked_by=["999"])


def test_dependency_links_are_bidirectional(store):
    t1 = store.create(title="upstream")
    t2 = store.create(title="downstream", blocked_by=[t1.id])
    assert t1.id in store.get(t2.id).blocked_by
    assert t2.id in store.get(t1.id).blocks


def test_claim_conflict_second_claim_fails(store):
    t = store.create(title="shared")
    store.update(t.id, assignee="alice", expected_assignee="")
    with pytest.raises(TaskConflictError) as exc:
        store.update(t.id, assignee="bob", expected_assignee="")
    assert "alice" in str(exc.value)


def test_expected_status_mismatch(store):
    t = store.create(title="t")
    with pytest.raises(TaskConflictError):
        store.update(t.id, status="in_progress", expected_status="blocked")


def test_cannot_start_blocked_task(store):
    t1 = store.create(title="upstream")
    t2 = store.create(title="downstream", blocked_by=[t1.id])
    with pytest.raises(TaskConflictError):
        store.update(t2.id, status="in_progress")
    store.update(t1.id, status="in_progress")
    store.update(t1.id, status="completed")
    assert store.update(t2.id, status="in_progress").status == "in_progress"


def test_completed_is_terminal_except_reopen(store):
    t = store.create(title="t")
    store.update(t.id, status="in_progress")
    store.update(t.id, status="completed")
    with pytest.raises(TaskConflictError):
        store.update(t.id, status="pending")
    assert store.update(t.id, status="in_progress").status == "in_progress"


def test_completed_unlocks(store):
    t1 = store.create(title="a")
    t2 = store.create(title="b")
    t3 = store.create(title="c", blocked_by=[t1.id, t2.id])
    store.update(t1.id, status="in_progress")
    store.update(t1.id, status="completed")
    assert store.completed_unlocks(t1.id) == []
    store.update(t2.id, status="in_progress")
    store.update(t2.id, status="completed")
    unlocked = store.completed_unlocks(t2.id)
    assert [t.id for t in unlocked] == [t3.id]


def test_cross_instance_visibility(tmp_path):
    path = tmp_path / "tasks.json"
    a = SharedTaskStore(path)
    b = SharedTaskStore(path)
    t = a.create(title="t")
    assert b.get(t.id) is not None
    a.update(t.id, status="in_progress")
    assert b.get(t.id).status == "in_progress"


def test_invalid_status_rejected(store):
    t = store.create(title="t")
    with pytest.raises(TaskConflictError):
        store.update(t.id, status="doing")
