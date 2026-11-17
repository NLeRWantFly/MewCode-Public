"""Coalesce host invalidations, then announce the current authoritative digest."""
from __future__ import annotations

import asyncio
import logging
import os

from mewcode.orchestration import task_view
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.task_design import design_view
from mewcode.orchestration.workbench_events import changed, subscribe


class Coalescer:
    """One trailing notification per task in each 250 ms window."""
    def __init__(self, read, send, *, async_read=None, call_later=None, spawn=None):
        self.read, self.send = read, send
        self.async_read = async_read
        self.call_later = call_later or (lambda delay, callback: asyncio.get_running_loop().call_later(delay, callback))
        self.spawn = spawn or asyncio.create_task
        self.pending, self.handles, self.versions = {}, {}, {}
        self.running, self.generation = {}, 0

    def touch(self, key, event):
        self.pending[key] = event
        if key not in self.handles and key not in self.running:
            self.handles[key] = self.call_later(.25, lambda: self.spawn(self.flush(key)))

    async def flush(self, key):
        if key in self.running:
            return
        self.handles.pop(key, None)
        event = self.pending.pop(key, None)
        if event is None:
            return
        generation = self.generation
        self.running[key] = generation
        try:
            payloads = await self.async_read(event) if self.async_read is not None else self.read(event)
            if generation != self.generation:
                return
            for payload in payloads:
                identity = (payload['conversation_id'], payload['task_id'], payload.get('run_id', ''))
                if self.versions.get(identity) == payload['version']:
                    continue
                await self.send(dict(type='workbench_changed', **payload))
                if generation != self.generation:
                    return
                self.versions[identity] = payload['version']
        except Exception:
            logging.getLogger(__name__).debug('Workbench refresh notification unavailable', exc_info=True)
        finally:
            if self.running.get(key) == generation:
                self.running.pop(key, None)
            if generation == self.generation and key in self.pending:
                self.touch(key, self.pending[key])

    def close(self):
        self.generation += 1
        for handle in self.handles.values():
            handle.cancel()
        self.pending.clear()
        self.handles.clear()
        self.running.clear()


def same_path(left, right):
    return bool(left and right and os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right)))


class WorkbenchNotifier:
    def __init__(self, workbench):
        self.workbench, self.srv, self.loop = workbench, workbench.srv, None
        self.coalescer = Coalescer(self.read, self.srv.WS_EVENTS.send_json, async_read=self.read_async)
        self.unsubscribe = subscribe(self.on_change)

    def attach_loop(self):
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            self.coalescer.close()
            self.coalescer.versions.clear()
            self.loop = loop

    def close(self):
        self.unsubscribe()
        self.coalescer.close()

    def on_change(self, event):
        if self.loop is None or self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(self.schedule, event)

    def schedule(self, event):
        kind, srv = event['kind'], self.srv
        if kind == 'run' and not same_path(event.get('root'), srv.EXPERT_RUNS.root):
            return
        if kind in {'draft', 'maintenance'} and not same_path(event.get('project'), srv.SESSION.work_dir):
            return
        if kind == 'team':
            host = getattr(srv.app.state, 'team_v2_host', None)
            if not host or not same_path(event.get('db_path'), host.state / 'coordination.sqlite3'):
                return
            key = ('team', event['db_path'])
        else:
            cid = event.get('conversation_id', '')
            if not cid:
                return
            run_id = event.get('run_id') or getattr(srv.SESSION.runtime_for(cid), 'expert_run_id', '')
            event = dict(event, run_id=run_id)
            key = (cid, run_id or event.get('task_id', ''))
        self.coalescer.touch(key, event)

    def observe(self, event):
        if event.get('type') in {'permission', 'human_oncall', 'ask_user', 'expert_role_wait',
                                'gate_timeout_selected', 'done', 'state', 'conversation_changed',
                                'workflow_activity', 'workflow_node_started', 'workflow_node_finished'}:
            cid = event.get('conversation_id') or self.srv._active_conversation_id()
            changed('runtime', conversation_id=cid, run_id=event.get('run_id', ''))

    async def read_async(self, event):
        # Run projections can scan large journals. Keep that read-only work
        # off the host loop, while preserving other stores' thread ownership.
        if event['kind'] == 'run':
            return await asyncio.to_thread(self.read, event)
        return self.read(event)

    def read(self, event):
        srv, wb = self.srv, self.workbench
        if event['kind'] == 'run' and event.get('version') and event.get('task_id'):
            return [{key: event[key] for key in ('conversation_id', 'task_id', 'run_id', 'version')}]
        if event['kind'] == 'team':
            host = getattr(srv.app.state, 'team_v2_host', None)
            if not host or not same_path(event.get('db_path'), host.state / 'coordination.sqlite3'):
                return []
            cid = srv._active_conversation_id()
            if not cid or not same_path(host.root, srv.SESSION.work_dir):
                return []
            # A project transaction can touch several runs (e.g. team control).
            views = [wb.team_view(dict(conversation_id=cid, run_id=row['run']['team_run_id'], _team_authorized=True), str(host.root))
                     for row in host.workbench_runs()]
        else:
            cid, run_id = event['conversation_id'], event.get('run_id', '')
            runtime = srv.SESSION.runtime_for(cid)
            project = getattr(runtime, 'work_dir', '') or srv.SESSION.work_dir
            if event.get('project') and not same_path(event['project'], project):
                return []
            state = DraftStore.read_state(project, cid) or {}
            design = design_view(state)
            record = srv.EXPERT_RUNS.read_workbench(run_id) if run_id else None
            if record:
                if record.get('conversation_id') != cid:
                    return []
                from mewcode.kernel.run_views import overlay
                view = wb.with_inbox(overlay(record['_workbench_view'], design), cid=cid, record=record, design=design, project=project)
            elif design:
                view = wb.design_card(project, cid, state, design)
            else:
                return []
            views = [view]
        return [dict(conversation_id=cid, task_id=view['task']['id'], run_id=view['task']['run_id'], version=view['version']) for view in views]
