"""A version-bound check with no changes must still permit explicit plan saving."""
import asyncio
import copy
import json
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, expect

from frontend import orch_drafts, server
from mewcode.orchestration.draft_sources import read_sources
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_generation import graph_state, ready_graph_document
from mewcode.orchestration.graph_local_repair import SCHEMA, unchanged_current_check, validate_document
from mewcode.orchestration.workflow_draft import normalize_legacy_document, seed_document
from tests.test_generic_loops import spec_for
from tests.test_graph_autobuild import make_app
from tests.test_workflow_drafts import api_env, ready

API = '/api/orchestration/graph-generation'


def prepare_checked_candidate(store, sources):
    document = seed_document(spec_for().as_dict())
    for entry in document['entries']:
        if entry['kind'] == 'node':
            entry['data'].update(outputs=['明确产物'], acceptance=['按证据核验'])
    assert validate_document(document, None, conversation_id='paper', revision=1)['valid']
    candidate = ready(store, sources, document)
    store.checkpoint(candidate, document, candidate['processed_ids'], 'ready', autobuild={
        'phase': 'review', 'status': 'needs_review', 'reason': 'review_error',
        'model_calls': 2, 'review': {'status': 'incomplete'},
    })
    return store.get()['candidate']


def checked_state(tmp_path):
    store = DraftStore(tmp_path, 'paper')
    candidate = prepare_checked_candidate(store, [{'id': 's1', 'role': 'user', 'text': '按证据核验'}])
    run_id = store.start_local_repair(candidate['id'], candidate['sequence'])
    packet = store.request_local_graph_repair(None)
    preview = store.preview_graph_repair(None, {
        'schema': SCHEMA, 'ticket': packet['ticket'], 'summary': '现有结构完整，无需修改', 'upserts': [],
    })
    assert not preview['changes'] and preview['validation']['valid']
    store.finish_local_repair(run_id, 'completed')
    state = store.get()
    state.update(project=str(tmp_path), conversation_id='paper', draft_id='main', running=False, progress=None,
                 coverage={'total': 1, 'processed': 1, 'pending': 0, 'history_changed': False}, basis={})
    return store, state


def test_unchanged_check_enables_preview_without_adopting_edits(tmp_path):
    store, state = checked_state(tmp_path)
    original = copy.deepcopy(state['candidate'])
    assert unchanged_current_check(state, None)
    projected = graph_state(state, None)
    assert projected['can_preview'] and projected['actions']['save']['enabled']
    assert not projected['execution_ready']
    assert projected['process']['outcome'] == 'checked'
    normalized, _ = normalize_legacy_document(original['document'])
    assert ready_graph_document(state, state['candidate'], None, conversation_id='paper') == normalized
    assert store.get()['candidate'] == original
    store.confirm(original['id'], original['sequence'])
    assert store.get()['document'] == normalized


@pytest.mark.parametrize('drift', ['sequence', 'document', 'base', 'history', 'warnings', 'unfinished', 'hidden_preview'])
def test_unchanged_check_cannot_reuse_stale_validation(tmp_path, drift):
    _, state = checked_state(tmp_path)
    base = None
    if drift == 'sequence':
        state['candidate']['sequence'] += 1
    elif drift == 'document':
        state['candidate']['document']['entries'][0]['body'] += ' 新要求'
    elif drift == 'base':
        base = {'nodes': [], 'edges': []}
    elif drift == 'history':
        state['source_ids'][0] = 'replacement'
    elif drift == 'warnings':
        state['warnings'] = ['来源读取失败']
    elif drift == 'unfinished':
        state['local_repair_run']['status'] = 'running'
    else:
        state['local_repair_preview'] = None
    assert not unchanged_current_check(state, base)
    assert not graph_state(state, base)['can_preview']


def noop_model(calls):
    async def model(_client, **kwargs):
        calls.append('request')
        payload = json.loads(kwargs['user_text'])
        return json.dumps({'schema': SCHEMA, 'ticket': payload['ticket'],
                           'summary': '现有结构完整，无需修改', 'upserts': []})
    return model


@pytest.mark.asyncio
async def test_noop_repair_api_preview_and_apply_survive_reload(api_env, monkeypatch):
    _, plans, path, owner = api_env
    sources, _, _ = read_sources(path)
    store = DraftStore(owner['project'], 'paper')
    original = prepare_checked_candidate(store, sources)
    calls = []
    monkeypatch.setattr(orch_drafts, 'collect_model_text', noop_model(calls))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url='http://test') as client:
        before = (await client.get(API, params=owner)).json()
        assert not before['can_preview']
        result = await client.post(API + '/repair', json={**owner, 'action': 'local'})
        assert result.status_code == 200, result.text
        assert result.json()['validation']['valid'] and not result.json()['changes']
        current = (await client.get(API, params=owner)).json()
        assert current['can_preview'] and current['actions']['save']['enabled']
        assert current['process']['outcome'] == 'checked'
        assert 'source_ids' not in current and 'processed_ids' not in current['candidate']
        assert store.get()['candidate'] == original and plans.get(owner['project']) is None
        preview = await client.post(API + '/graph', json={**owner, 'action': 'preview'})
        assert preview.status_code == 200, preview.text
        saved = await client.post(API + '/graph', json={**owner, 'action': 'apply', 'preview_id': preview.json()['id']})
        assert saved.status_code == 200, saved.text
        assert server.PipelineStore(owner['project']).get_pipeline(saved.json()['graph_id']) is not None
        assert store.get()['candidate'] is None
        reloaded = (await client.get(API, params=owner)).json()
        assert reloaded['can_preview'] and not reloaded['execution_ready']
        assert reloaded['process']['outcome'] == 'checked'
    assert len(calls) == 1
