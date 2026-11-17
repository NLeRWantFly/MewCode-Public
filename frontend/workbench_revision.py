"""User-message revision drafts; confirmation records one atomic user proposal."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from frontend.prompt_dispatch import PromptError
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.events import digest
from mewcode.orchestration.collab_coordinator import CollaborationCoordinator
from mewcode.orchestration.collab_service import bound_record, revision_changes
from mewcode.orchestration.draft_sources import read_sources
from mewcode.orchestration.draft_store import DraftStore, now
from mewcode.orchestration.task_design import TaskDesign
from mewcode.orchestration.task_view import _requirements

STALE_REVISION = '要求已被更新，请在最新版本上重新修改'
NOT_REVISION = '这条消息没有识别出需要修改的要求'
ACTIVE = {'running', 'paused', 'blocked', 'needs_attention'}


def active_record(srv, project, cid):
    design = (DraftStore.read_state(project, cid) or {}).get('task_design')
    record = bound_record(srv.EXPERT_RUNS, design, cid)
    if not record or record.get('status') not in ACTIVE:
        raise ValueError('当前对话没有正在处理的协作任务')
    return design, record


def sources(srv, project, cid):
    session = srv.SESSION.session
    if session is None or getattr(session, 'session_id', cid) != cid:
        raise ValueError('请回到消息所属对话')
    directory = getattr(session, '_sessions_dir', None) or Path(project) / '.mewcode' / 'sessions'
    rows, _, _ = read_sources(Path(directory) / (cid + '.jsonl'))
    grouped = {}
    for row in rows:
        if row['role'] == 'user' and row['kind'] == 'message' and not row.get('purpose'):
            message = grouped.setdefault(row['line'], dict(id=row['id'], source_ids=[], text='', role='user', at=row['at']))
            message['source_ids'].append(row['id'])
            message['text'] += row['text']
    return list(grouped.values())


def message(srv, project, cid, message_id):
    if not isinstance(message_id, str):
        raise ValueError('请选择一条用户原始消息')
    source = next((row for row in sources(srv, project, cid) if message_id in row['source_ids']), None)
    if not source:
        raise ValueError('找不到这条用户原始消息')
    if len(source['text']) > 16000:
        raise ValueError('这条消息超过修订上下文上限，请明确一条更短的修改要求')
    return source


def draft_digest(draft):
    return digest({key: draft[key] for key in ('id', 'task_id', 'run_id', 'message', 'base_version', 'epoch', 'entries_basis', 'changes', 'summary')})


class RevisionDrafts:
    def __init__(self, workbench):
        self.workbench, self.srv, self.locks = workbench, workbench.srv, {}

    async def coordinator(self, record, cid):
        catalog = await self.srv.MODEL_REGISTRY.catalog(local_only=True)
        async def ask(model, payload, system):
            from mewcode.kernel.arbitration import ask_client
            runtime = self.srv.SESSION.runtime_for(cid)
            client = getattr(runtime, 'client', None) or getattr(self.srv.SESSION, 'client', None)
            usage = getattr(client, 'get_usage_recorder', lambda: None)()
            resolved = self.srv.MODEL_REGISTRY.resolve(model, usage_recorder=usage)
            return await ask_client(resolved.client, payload, system)
        return CollaborationCoordinator(self.srv.EXPERT_RUNS, record['run_id'], catalog, ask=ask)

    def current(self, body):
        project, cid = self.workbench.scope(body)
        design, record = active_record(self.srv, project, cid)
        if (body.get('task_id') not in {None, '', design['task_id']}
                or body.get('run_id') not in {None, '', record['run_id']}):
            raise ValueError('任务与运行绑定已变化，请重新选择任务。')
        return project, cid, design, record

    async def create(self, body):
        project, cid, design, record = self.current(body)
        body = dict(body, task_id=design['task_id'], run_id=record['run_id'])
        original = message(self.srv, project, cid, body.get('message_id'))
        identity = digest([design['task_id'], record['run_id'], original['id']])
        async with self.locks.setdefault(identity, asyncio.Lock()):
            project, cid, design, record = self.current(body)
            previous = (record.get('workbench_revision_drafts') or {}).get(identity)
            if previous:
                return copy.deepcopy(previous)
            value = ledger.state(record)
            entries = [row for row in _requirements(value) if row['editable']]
            draft = dict(id=identity, task_id=design['task_id'], run_id=record['run_id'], conversation_id=cid,
                         message=original, base_version=value['spec_version'], epoch=value['coordinator_epoch'],
                         entries_basis=digest(value['entries']), status='preparing', changes=[], summary='', created_at=now())
            coordinator = await self.coordinator(record, cid)
            self.current(body)
            if message(self.srv, project, cid, original['id']) != original:
                raise ValueError('消息来源已变化，请重新选择')
            def reserve(saved):
                current = ledger.state(saved)
                if (current['spec_version'] != draft['base_version'] or current['coordinator_epoch'] != draft['epoch']
                        or digest(current['entries']) != draft['entries_basis'] or saved.get('status') not in ACTIVE):
                    raise ValueError(STALE_REVISION)
                saved.setdefault('workbench_revision_drafts', {})[identity] = copy.deepcopy(draft)
            self.srv.EXPERT_RUNS.mutate(record['run_id'], reserve, durable=True)
            try:
                result = await coordinator.draft_revision(draft_id=identity, message=original, entries=entries)
                self.current(body)
                def finish(saved):
                    current, row = ledger.state(saved), saved['workbench_revision_drafts'][identity]
                    if (current['spec_version'] != row['base_version'] or current['coordinator_epoch'] != row['epoch']
                            or digest(current['entries']) != row['entries_basis']):
                        raise ValueError(STALE_REVISION)
                    if result['action'] == 'revise':
                        revision_changes(current, result['changes'], identity)
                    row.update(changes=copy.deepcopy(result['changes']), summary=result['summary'],
                               status='pending' if result['action'] == 'revise' else 'not_revision')
                    row['digest'] = draft_digest(row)
                    if row['status'] == 'not_revision':
                        row['message_text'] = NOT_REVISION
                        ledger.event(current, 'revision_draft_not_revision', draft_id=identity, actor='coordinator',
                                     reason=NOT_REVISION, message_id=original['id'], time=now())
                saved = self.srv.EXPERT_RUNS.mutate(record['run_id'], finish, durable=True)
                return copy.deepcopy(saved['workbench_revision_drafts'][identity])
            except Exception as exc:
                error = str(exc)
                self.srv.EXPERT_RUNS.mutate(record['run_id'], lambda saved: saved['workbench_revision_drafts'][identity].update(
                    status='failed', error=error), durable=True)
                raise


async def answer_draft(workbench, item, body):
    srv, context = workbench.srv, item['context']
    project, cid = workbench.scope(body)
    design, record = active_record(srv, project, cid)
    if record['run_id'] != item['run_id'] or design['task_id'] != item['task_id']:
        raise PromptError()
    if (body.get('draft_id') != context['id'] or body.get('draft_digest') != context['digest']
            or body.get('base_version') != context['base_version']):
        raise PromptError()
    action = body.get('action') or (body.get('answers') or {}).get('action')
    if action not in {'accept', 'decline'}:
        raise PromptError('请选择是否采用这些修改', 400)
    identity = context['id']
    def apply(saved):
        value = ledger.state(saved)
        if saved.get('status') not in ACTIVE or value['task_id'] != item['task_id']:
            raise PromptError()
        draft = (saved.get('workbench_revision_drafts') or {}).get(identity)
        if (not draft or draft['status'] != 'pending' or draft.get('digest') != body['draft_digest']
                or draft_digest(draft) != body['draft_digest']):
            raise PromptError()
        if action == 'decline':
            draft.update(status='rejected', answered_at=now())
            ledger.event(value, 'revision_draft_rejected', draft_id=identity, actor='user', time=now(),
                         reason='用户未采用这份修订草稿', message_id=draft['message']['id'])
            return
        if (value['spec_version'] != draft['base_version'] or value['coordinator_epoch'] != draft['epoch']
                or digest(value['entries']) != draft['entries_basis']):
            raise ValueError(STALE_REVISION)
        # No mutation before every field has passed the same user-revision guards.
        changes = revision_changes(value, draft['changes'], identity)
        source_id, confirmation_id = 'message:' + draft['message']['id'], 'draft_confirmation:' + identity
        proposal_id = digest([saved['run_id'], identity])
        ledger.proposal(value, proposal_id=proposal_id, changes=changes, evidence=[source_id, confirmation_id],
                        actor='user', base_version=draft['base_version'], epoch=draft['epoch'])
        value.setdefault('user_sources', {})[source_id] = dict(role='user', text=draft['message']['text'],
            message_id=draft['message']['id'], source_ids=draft['message']['source_ids'], kind='original_message')
        value['user_sources'][confirmation_id] = dict(role='user', text='用户在任务工作台确认了这份修订草稿',
            draft_id=identity, digest=draft['digest'], kind='confirmation', time=now())
        draft.update(status='accepted', proposal_id=proposal_id, answered_at=now())
        saved.update(status='paused', status_reason='要求修订已保存，等待核对后继续。')
    updated = srv.EXPERT_RUNS.mutate(record['run_id'], apply, durable=True)
    if action == 'accept':
        if srv.SESSION.chat_busy(cid):
            await srv.request_chat_interrupt(cid, action='pause')
        latest = TaskDesign(project, cid).read()
        if latest and latest['task_id'] == design['task_id'] and latest.get('status') != 'paused':
            TaskDesign(project, cid).transition(latest, 'paused', '要求修订已保存，继续任务时核对并采用新规格。')
    return dict(ok=True, draft=updated['workbench_revision_drafts'][identity])


def sync_tool(srv, *, agent=None, project=None, cid=None):
    from mewcode.tools.revision_draft import SubmitRevisionDraft
    agent = agent or getattr(srv.SESSION, 'agent', None)
    if not agent or not getattr(agent, 'registry', None):
        return
    project, cid = project or srv.SESSION.work_dir, cid or srv._active_conversation_id()
    try:
        active_record(srv, project, cid)
    except (ValueError, OSError, TypeError):
        remove = getattr(agent, '_remove_runtime_tool', None) or getattr(agent.registry, 'unregister', None)
        if remove is not None:
            remove(SubmitRevisionDraft.name)
        return
    async def submit(message_id):
        if agent is not getattr(srv.SESSION, 'agent', None):
            raise ValueError('主对话已切换，请重新选择消息')
        return await srv.WORKBENCH_REVISIONS.create(dict(project=str(project), conversation_id=cid, message_id=message_id))
    installer = getattr(agent, '_install_runtime_tool', agent.registry.register)
    installer(SubmitRevisionDraft(submit))


def install(app, workbench):
    service = workbench.srv.WORKBENCH_REVISIONS = RevisionDrafts(workbench)
    @app.post('/api/workbench/revision-drafts')
    async def create(request: Request):
        origin = request.headers.get('origin')
        if (request.headers.get('x-mew-workbench') != '1' or request.headers.get('sec-fetch-site') == 'cross-site'
                or origin and origin != str(request.base_url).rstrip('/')):
            return JSONResponse({'error': '请从当前任务工作台提交修订草稿'}, status_code=403)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {'project', 'conversation_id', 'task_id', 'run_id', 'message_id'}:
                raise ValueError('修订草稿只能引用用户消息身份')
            return {'draft': await service.create(body)}
        except (ValueError, OSError, TypeError, RuntimeError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)

    @app.get('/api/workbench/revision-sources')
    async def revision_sources(request: Request):
        try:
            project, cid, _, _ = service.current(dict(request.query_params))
            return {'messages': sources(workbench.srv, project, cid)}
        except (ValueError, OSError, TypeError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)
