"""Candidate dependencies remain executable inputs, never writable deliveries."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.experts.audit_workspace import AuditWorkspace, manifest
from mewcode.experts import candidate_runtime
from mewcode.kernel.shadow_policy import bind_collaboration_candidate
from mewcode.permissions.checker import Decision
from mewcode.sandbox.windows import NativeBoundaryUnavailable


def project(tmp_path):
    root = tmp_path / 'project'
    (root / '.venv' / 'Scripts').mkdir(parents=True)
    (root / '.venv' / 'pyvenv.cfg').write_text('home = existing-runtime\n')
    (root / '.venv' / 'Scripts' / 'python.exe').write_bytes(b'existing-interpreter')
    (root / 'result.txt').write_text('before')
    return root


def candidate(root):
    return AuditWorkspace(root, root / '.mewcode' / 'collaboration-candidates',
                          control_archive=True, freeze_python=True)


def test_existing_runtime_is_copied_bound_on_resume_and_never_published(tmp_path):
    root = project(tmp_path)
    tx = candidate(root)
    assert (tx.candidate / '.venv/Scripts/python.exe').read_bytes() == b'existing-interpreter'
    assert (tx.candidate / '.venv/Scripts/python.exe').stat().st_ino != (root / '.venv/Scripts/python.exe').stat().st_ino
    assert all(not key.startswith('.venv/') for key in manifest(tx.candidate))
    restored = AuditWorkspace.resume(root, tx.root.parent, str(tx.root))
    assert restored.runtime_inputs == tx.runtime_inputs
    (restored.candidate / 'result.txt').write_text('repaired')
    restored.seal()
    assert restored.promote() == ['result.txt']
    assert (root / '.venv/Scripts/python.exe').read_bytes() == b'existing-interpreter'


@pytest.mark.parametrize('where', ['source', 'candidate'])
def test_changed_runtime_rejects_resume_and_seal(tmp_path, where):
    root = project(tmp_path)
    tx = candidate(root)
    (getattr(tx, where) / '.venv/pyvenv.cfg').write_text('home = other')
    with pytest.raises(ValueError, match='Python'):
        AuditWorkspace.resume(root, tx.root.parent, str(tx.root))
    with pytest.raises(ValueError, match='Python'):
        tx.seal()


def test_copy_rejects_hardlinks_and_has_a_byte_budget(tmp_path, monkeypatch):
    root = project(tmp_path)
    os.link(root / '.venv/pyvenv.cfg', root / '.venv/linked.cfg')
    with pytest.raises(NativeBoundaryUnavailable, match='hard-linked'):
        candidate(root)
    (root / '.venv/linked.cfg').unlink()
    monkeypatch.setattr(candidate_runtime, 'MAX_BYTES', 2)
    with pytest.raises(NativeBoundaryUnavailable, match='budget'):
        candidate(root)


def test_candidate_file_tools_cannot_modify_runtime_even_if_parent_allows(tmp_path):
    root = project(tmp_path)
    tx = candidate(root)
    checker = SimpleNamespace(check=lambda *args: Decision('allow', 'test'))
    parent = SimpleNamespace(check=lambda *args: Decision('allow', 'test'))
    bind_collaboration_candidate(checker, parent, {'source': str(root), 'candidate': str(tx.candidate), 'baseline': str(tx.baseline)})
    for name in ('WriteFile', 'EditFile'):
        assert checker.check(SimpleNamespace(name=name, category='write'), {'file_path': '.venv/pyvenv.cfg'}).effect == 'deny'
    assert checker.check(SimpleNamespace(name='ReadFile', category='read'), {'file_path': '.venv/pyvenv.cfg'}).effect == 'allow'


def test_missing_environment_is_not_created_or_substituted(tmp_path):
    root = tmp_path / 'empty'
    root.mkdir()
    tx = candidate(root)
    assert tx.runtime_inputs == {}
    assert not (tx.candidate / '.venv').exists()


def test_patch_repair_context_comes_from_saved_decision_and_failed_checks():
    from mewcode.orchestration.collab_patch import PatchCandidate
    patch = PatchCandidate.__new__(PatchCandidate)
    saved = {'baseline_id': 'failed-attempt', 'decision': {'instructions': 'fix exact boundary'},
             'baseline_report': {'checks': [{'id': 'limit', 'passed': False, 'evidence': {'issue': 'oversize single page'}},
                                             {'id': 'retained', 'passed': True}]}}
    patch.saved = lambda: saved
    context = json.loads(patch.execution_context().split('PATCH_CONTEXT_JSON\n')[1])
    assert context['decision'] == saved['decision']
    assert context['failed_checks'] == saved['baseline_report']['checks'][:1]
    assert patch.execution_context() == patch.execution_context()
    patch.attempt_id = 'candidate'
    value = {'spec_version': 1, 'attempts': {'candidate': {'contract_id': 'work'}},
             'timeline': [{'kind': 'retry_authorized', 'contracts': ['work'], 'decision_id': 'current'},
                          {'kind': 'retry_authorized', 'contracts': ['other'], 'decision_id': 'unrelated'}],
             'human_decisions': {'current': {'spec_version': 1, 'payload': {'reason': 'Read prepared repair inputs'}}}}
    context = json.loads(patch.execution_context(value).split('PATCH_CONTEXT_JSON\n')[1])
    assert context['authorized_recovery'] == value['human_decisions']['current']
