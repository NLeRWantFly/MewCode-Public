"""Desktop workbench reads; host-scoped, model-free and side-effect-free."""
from __future__ import annotations

import asyncio
import os
import sqlite3

from fastapi import Request
from fastapi.responses import JSONResponse

from mewcode.orchestration import task_view, inbox
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.task_design import design_view
from mewcode.teams.store_v2 import TeamStateError


class Workbench:
    def __init__(self, srv):
        self.srv = srv

    def scope(self, values):
        project = self.srv.SESSION.work_dir
        cid = self.srv._active_conversation_id()
        if not cid or values.get("conversation_id") != cid:
            raise ValueError("项目或对话已切换，请回到任务所属对话。")
        supplied = values.get("project")
        if supplied and os.path.normcase(os.path.abspath(supplied)) != os.path.normcase(os.path.abspath(project)):
            raise ValueError("项目或对话已切换，请回到任务所属对话。")
        return str(project), cid

    def context(self, values):
        project, cid = self.scope(values)
        state = DraftStore.read_state(project, cid) or {}
        return project, cid, state, design_view(state)

    def records(self, cid):
        return [record for run_id in self.srv.EXPERT_RUNS.query_run_ids(cid)
                if (record := self.srv.EXPERT_RUNS.read_workbench(run_id))]

    def team_host(self, values, project):
        host = getattr(self.srv.app.state, 'team_v2_host', None)
        if not host or not values.get('_team_authorized') or not host.enabled:
            return None
        if os.path.normcase(os.path.realpath(host.root)) != os.path.normcase(os.path.realpath(project)):
            return None
        return host

    def team_view(self, values, project):
        host = self.team_host(values, project)
        if host is None:
            raise LookupError('找不到这个团队运行')
        run_id, task_id = values.get('run_id'), values.get('task_id')
        if not run_id:
            match = next((row for row in host.workbench_runs() if task_view.team_header(row)['id'] == task_id), None)
            if not match:
                raise LookupError('找不到这个团队任务')
            run_id = match['run']['team_run_id']
        snapshot = host.snapshot(run_id, read_only=True)
        if task_id and task_view.team_header(snapshot)['id'] != task_id:
            raise ValueError('任务与运行绑定已变化，请重新选择任务。')
        view = task_view.team_view(snapshot)
        view['inbox'] = inbox.collect(dict(view['task'], conversation_id=values['conversation_id']), team_items=view['inbox'])
        return task_view.finish(view)

    def startup_choices(self, task, cid):
        cached = getattr(self.srv, '_interaction_choice_state', {}) or {}
        return [row for key, row in (cached.get('startup') or {}).items() if key[0] == cid and row.get('task_id') == task['id']]

    def with_inbox(self, view, *, cid, record=None, design=None, draft_state=None, project=None):
        if view['task'].get('purpose') == 'exploration':
            view['inbox'] = inbox.collect(view['task'], runtime=self.srv.SESSION.runtime_for(cid))
            return task_view.finish(view)
        if view['task'].get('mode') == 'workflow' and view['task'].get('run_id'):
            from mewcode.workflow_core.activity import observe_view
            from mewcode.workflow_core.runtime import observation
            runtime = self.srv.SESSION.runtime_for(cid)
            source = self.srv.EXPERT_RUNS.read_execution_fields(view['task']['run_id'],
                'run_id', 'node_states', 'locked_flow', 'kernel_gates')
            view['inbox'] = inbox.collect(view['task'], record=source, runtime=runtime)
            live, fault = observation(view['task']['run_id'])
            observe_view(view, live=live, fault=fault)
            return task_view.finish(view)
        if view['legacy']:
            return view
        runtime = self.srv.SESSION.runtime_for(cid)
        project = project or self.srv.SESSION.work_dir
        record = record or {}
        flow_id = (record.get('locked_flow') or {}).get('flow_id') or record.get('flow_id')
        lessons = self.srv.EXPERT_LESSONS.list(flow_id) if flow_id else []
        view['inbox'] = inbox.collect(view['task'], record=record, runtime=runtime, design=design, lessons=lessons,
                                     startup=self.startup_choices(view['task'], cid) if not record else [])
        if '_workbench_view' in record:
            live_ids = {row.get('source_id') for row in view['inbox']}
            completed_ids = set()
            for kind in ('human_oncall', 'ask_user'):
                request = getattr(runtime, kind + '_request', None)
                if request is not None and request.future.done():
                    completed_ids.add(request.request_id)
            permission = getattr(runtime, 'permission_future', None)
            if permission is not None and permission.done():
                completed_ids.add((getattr(runtime, 'permission_request', None) or {}).get('request_id'))
            view['inbox'] = [row for row in record.get('static_inbox', [])
                             if row.get('source_id') not in live_ids | completed_ids] + view['inbox']
        if not record:
            from frontend.workbench_startup import collect
            startup = collect(self.srv, project, cid, view['task'], draft_state or {})
            if startup:
                view['inbox'] = [row for row in view['inbox'] if row['type'] != 'run_inputs'] + startup
        candidate = (draft_state or {}).get('candidate')
        if not record and candidate:
            cached = ((getattr(self.srv, '_interaction_choice_state', {}) or {}).get('graph') or {}).get(
                (cid, candidate.get('id'), candidate.get('sequence')))
            view['inbox'].extend(inbox.design_choices(view['task'], candidate, cached))
        from frontend.workbench_maintenance import project as maintenance_projection
        view['maintenance'], decisions = maintenance_projection(self.srv, project, cid, view['task'], record)
        view['inbox'].extend(decisions)
        from frontend.workbench_controls import collect as run_controls
        view['inbox'].extend(run_controls(self.srv, view['task'], record))
        view['inbox_resolved'] = True
        return task_view.finish(view)

    def select(self, values, *, read_only=False):
        project, cid, state, design = self.context(values)
        run_id, task_id = values.get("run_id"), values.get("task_id")
        if not run_id and not task_id:
            from mewcode.orchestration.chat_routing import dialogue_focus
            from mewcode.workflow_core.schema import digest
            runtime = self.srv.SESSION.runtime_for(cid) or self.srv.SESSION
            focus = dialogue_focus(runtime)
            if focus.get('kind') == 'chat' and focus.get('task_id'):
                candidate = focus.get('workflow_view_run_id') or digest([cid, focus['task_id'], 'trace'])[:32]
                source = self.srv.EXPERT_RUNS.read_execution_fields(candidate, 'run_id', 'conversation_id', 'host_execution_scope')
                if source.get('conversation_id') == cid and (source.get('host_execution_scope') or {}).get('task_id') == focus['task_id']:
                    run_id = candidate
        if run_id:
            record = (self.srv.EXPERT_RUNS.read_workbench(run_id) if read_only
                      else self.srv.EXPERT_RUNS.load(run_id))
            if not record or record.get("conversation_id") != cid:
                raise LookupError("找不到这个任务运行")
            identity = record['_workbench_view']['task']['id'] if '_workbench_view' in record else task_view.identity(record, design)
            if task_id and identity != task_id:
                raise ValueError("任务与运行绑定已变化，请重新选择任务。")
            return project, cid, state, design, record
        if read_only and not task_id and design and design.get('run_id'):
            # The default page already has an exact run binding. Reading
            # every historical run here makes refresh cost grow with history.
            record = self.srv.EXPERT_RUNS.read_workbench(design['run_id'])
            if record and record.get('conversation_id') != cid:
                record = None
            return project, cid, state, design, record
        records = self.records(cid)
        if task_id:
            record = next((row for row in records if row['_workbench_view']['task']['id'] == task_id), None)
            if not record and (not design or design["task_id"] != task_id):
                raise LookupError("找不到这个任务")
        elif design:
            record = next((row for row in records if row["run_id"] == design.get("run_id")), None)
        else:
            record = records[0] if records else None
        if record and not read_only:
            record = self.srv.EXPERT_RUNS.load(record['run_id'])
        return project, cid, state, design, record

    def view(self, values):
        if str(values.get('run_id') or '').startswith('tr-') or str(values.get('task_id') or '').startswith('team:'):
            project, _ = self.scope(values)
            return self.team_view(values, project)
        project, cid, state, design, record = self.select(values, read_only=True)
        if not values.get('run_id') and not values.get('task_id'):
            from mewcode.orchestration.chat_routing import dialogue_focus
            runtime = self.srv.SESSION.runtime_for(cid) or self.srv.SESSION
            focus = dialogue_focus(runtime)
            selected = (record or {}).get('_workbench_view', {}).get('task', {})
            if (focus.get('kind') == 'chat' and focus.get('task_id')
                    and (selected.get('id') != focus['task_id'] or selected.get('purpose') == 'delegation')):
                from mewcode.workflow_core.projection import view as workflow_view
                from mewcode.workflow_core.state import fresh
                from mewcode.workflow_core.trace import PROFILE
                current = fresh(dict(execution_profile=PROFILE, label=focus.get('task') or '当前对话',
                                     goal=focus.get('task') or '', nodes=[], edges=[]))
                current['status'] = 'running' if self.srv.SESSION.chat_busy(cid) else 'idle'
                display = workflow_view(dict(run_id='', conversation_id=cid, workflow_core=current))
                display['task'].update(id=focus['task_id'],
                    status='处理中' if current['status'] == 'running' else '当前对话',
                    current_activity='主对话推进任务，公开步骤会随探索显示。')
                return self.with_inbox(display, cid=cid)
        if record:
            from mewcode.kernel.run_views import overlay
            return self.with_inbox(overlay(record['_workbench_view'], design, runtime=record),
                                   cid=cid, record=record, design=design, project=project)
        if not design:
            return None
        return self.design_card(project, cid, state, design)

    def design_card(self, project, cid, state, design):
        """One read-only draft projection for the page and its notification."""
        entries = design["specification"]["entries"]
        if design.get("status") == "ready" and (design.get("build_request") or {}).get("state") == "saved":
            from frontend.workbench_startup import saved_spec
            from mewcode.orchestration.workflow_draft import seed_document
            spec = saved_spec(self.srv, project, cid, design["task_id"])
            if spec:
                entries = seed_document(spec, previous=state.get("document"))["entries"]
        view = self.with_inbox(task_view.design_view(design, conversation_id=cid, entries=entries),
                              cid=cid, design=design, draft_state=state, project=project)
        from frontend.workbench_editor import base_spec
        from mewcode.workflow_core.planning import design_projection
        design_projection(view, state, base_spec(self, project, cid, state), cid)
        return task_view.finish(view)

    def tasks(self, values):
        project, cid, state, design = self.context(values)
        records = self.records(cid)
        from mewcode.kernel.run_views import overlay
        from mewcode.workflow_core.activity import observe_view
        from mewcode.workflow_core.runtime import observation
        tasks = []
        for row in records:
            view = overlay(row['_workbench_view'], design, runtime=row)
            live, fault = observation(row['run_id'])
            observe_view(view, live=live, fault=fault)
            tasks.append(task_view.finish(view)['task'])
        if design and not any(row["id"] == design["task_id"] and row["run_id"] == design.get("run_id", "") for row in tasks):
            tasks.insert(0, task_view.design_view(design, conversation_id=cid)["task"])
        host = self.team_host(values, project)
        if host:
            tasks.extend(task_view.team_header(row) for row in host.workbench_runs())
        return dict(conversation_id=cid, tasks=tasks)


def install(app, srv):
    workbench = Workbench(srv)
    srv.WORKBENCH = workbench
    from frontend.workbench_notify import WorkbenchNotifier
    previous = getattr(srv, 'WORKBENCH_NOTIFIER', None)
    if previous is not None:
        previous.close()
    srv.WORKBENCH_NOTIFIER = WorkbenchNotifier(workbench)

    async def response(call, request):
        srv.WORKBENCH_NOTIFIER.attach_loop()
        try:
            values = dict(request.query_params)
            authorize = getattr(app.state, 'team_v2_authorize', None)
            values['_team_authorized'] = bool(authorize and authorize(request))
            owner = workbench.scope(values)
            values['project'] = owner[0]
            def render():
                return JSONResponse(call(values), headers={"Cache-Control": "no-store"})
            # Team hosts retain their connection's owner thread. Ordinary
            # display stores open their own read-only connections per call.
            team = (str(values.get('run_id') or '').startswith('tr-')
                    or str(values.get('task_id') or '').startswith('team:')
                    or workbench.team_host(values, owner[0]) is not None)
            result = render() if team else await asyncio.to_thread(render)
            if workbench.scope(values) != owner:
                raise ValueError('项目或对话已切换，请回到任务所属对话。')
            return result
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (ValueError, OSError, TypeError, TeamStateError, sqlite3.Error) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get("/api/workbench/view")
    async def view(request: Request):
        return await response(workbench.view, request)

    @app.get("/api/workbench/tasks")
    async def tasks(request: Request):
        return await response(workbench.tasks, request)

    from frontend.workbench_answers import install_answers
    install_answers(app, workbench)
    from frontend.workbench_revision import install as install_revisions
    install_revisions(app, workbench)
    from frontend.workbench_maintenance import install as install_maintenance
    install_maintenance(app, workbench)
    from frontend.workbench_controls import install as install_controls
    install_controls(app, workbench)
    from frontend.workbench_editor import install as install_editor
    install_editor(app, workbench)
    from frontend.workflow_core_api import install as install_workflows
    install_workflows(app, workbench)
    from frontend.workbench_export import install as install_export
    install_export(app, workbench)
    from frontend.workbench_connections import install as install_connections
    install_connections(app)

    return workbench
