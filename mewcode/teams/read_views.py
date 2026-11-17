"""Read the existing team store without initializing schemas or model routes.

Reuse the host's runtime and responsibility views on a SQLite read-only
transaction. Opening the workbench never enables teams or registers providers.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from types import SimpleNamespace

from .dynamic_plans import PlanService
from .orchestration_views import OrchestrationViews
from .store_v2 import TeamStoreV2


@contextmanager
def reader(host):
    path = host.state / 'coordination.sqlite3'
    if not host.enabled or not path.is_file():
        yield None
        return
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        store = SimpleNamespace(_conn=connection, _lock=threading.RLock())
        yield TeamStoreV2(store, read_only=True)
    finally:
        connection.close()


def runs(host):
    with reader(host) as repository:
        if repository is None:
            return []
        rows = repository._conn.execute('SELECT r.team_run_id FROM team_v2_runs r JOIN team_v2_instances i USING(team_id) WHERE i.project_id=? ORDER BY r.created_at DESC,r.team_run_id', (host.actor.project_id,)).fetchall()
        result = []
        for row in rows:
            view = base_snapshot(repository, host.actor, row[0])
            result.append(dict(team=view['team'], run=view['run'], task_count=len(view['tasks'])))
        return result


def base_snapshot(repository, actor, run_id):
    if repository._conn.execute("SELECT 1 FROM sqlite_master WHERE name='team_v2_runtime_schema'").fetchone():
        return repository.runtime.snapshot(actor, run_id)
    row = repository._conn.execute('SELECT team_id FROM team_v2_runs WHERE team_run_id=?', (run_id,)).fetchone()
    if row is None:
        raise LookupError('找不到这个团队运行')
    team = repository.get_team(actor, row['team_id'])
    repository._team(actor, team['team_id'], owner=True)
    run = repository.get_run(actor, team['team_id'], run_id)
    return dict(team=team, run=run['run'], definition=run['compiled']['spec'], tasks=[], historical_tasks=[],
                histories={}, artifacts=[], publications=[], events=[], orchestration={}, runtime_uninitialized=True)


def snapshot(host, run_id):
    with reader(host) as repository:
        if repository is None:
            raise LookupError('找不到这个团队运行')
        result = base_snapshot(repository, host.actor, run_id)
        if result.get('runtime_uninitialized'):
            return result
        runtime = repository.runtime
        views = OrchestrationViews(runtime)
        result['definition'] = runtime._compiled(run_id).spec.model_dump(mode='json')
        result['orchestration'] = views.summary(host.actor, run_id)
        result['orchestration']['plan_proposals'] = PlanService(SimpleNamespace(repository=runtime.repo)).list(host.actor, run_id)
        result['histories'] = {task['task_id']: views.task_history(host.actor, task['task_id'], limit=30)
                               for task in [*result['tasks'], *result['historical_tasks']]}
        # Read every event page within this transaction, in stable journal order.
        result['events'], cursor = [], 0
        while True:
            page = views.events(host.actor, run_id, after=cursor, limit=200)
            result['events'].extend(page['events'])
            cursor = page['next_cursor']
            if not page['has_more']:
                break
        result['event_cursor'] = cursor
        # A publication names a fixed manifest, not a mutable workspace path.
        for publication in result['publications']:
            row = runtime.db.execute('SELECT * FROM team_v2_publications WHERE publication_id=?', (publication['publication_id'],)).fetchone()
            publication['manifest'] = [{key: value for key, value in entry.items() if key != 'before_b64'}
                                       for entry in json.loads(row['journal_json'])['entries']]
        result['artifacts'] = [dict(runtime.artifact(host.actor, row['version_id'], include_content=False), producer_task_id=row['producer_task_id'])
                               for row in runtime.db.execute('SELECT version_id,producer_task_id FROM team_v2_artifacts WHERE team_run_id=? AND producer_task_id IS NOT NULL ORDER BY created_at,version_id', (run_id,))]
        return result
