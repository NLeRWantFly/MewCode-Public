"""Discover separately hosted local workflows without opening a second run store."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid

import httpx

from mewcode.runtime_paths import user_data_root
from .projection import LABELS


def origin(value):
    url = urlsplit(str(value))
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
            or url.username or url.password or url.path not in {'', '/'} or url.query or url.fragment):
        raise ValueError('工作流连接必须使用本机 127.0.0.1 服务地址')
    return f'http://127.0.0.1:{url.port}'


def registry(profile=None):
    return Path(profile if profile is not None else user_data_root()) / 'workflow-connections-v1'


def register(*, url, run_id, conversation_id, project, title, profile=None):
    if not re.fullmatch(r'[a-f0-9]{32}', run_id):
        raise ValueError('Invalid workflow run identity')
    project = Path(project)
    if not project.is_absolute() or not conversation_id:
        raise ValueError('A connection requires its project and conversation')
    value = dict(schema_version=1, id=run_id, url=origin(url), run_id=run_id,
                 conversation_id=conversation_id, project=str(project.resolve()), title=str(title)[:200],
                 registered_at=time.time())
    root = registry(profile)
    root.mkdir(parents=True, exist_ok=True)
    target = root / (run_id + '.json')
    temporary = root / (run_id + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, target)
    return value


def descriptors(profile=None):
    root = registry(profile)
    if not root.is_dir():
        return []
    result = []
    for path in sorted(root.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:32]:
        try:
            if path.stat().st_size > 16_384:
                continue
            item = json.loads(path.read_text(encoding='utf-8'))
            if (item.get('schema_version') != 1 or not re.fullmatch(r'[a-f0-9]{32}', str(item.get('run_id', '')))
                    or item.get('id') != path.stem or item['run_id'] != path.stem
                    or not isinstance(item.get('title'), str) or not isinstance(item.get('conversation_id'), str)
                    or not item['conversation_id'] or not Path(item.get('project', '')).is_absolute()):
                continue
            item['url'] = origin(item['url'])
            result.append(item)
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            continue
    return result


async def inspect_connection(client, item):
    result = {key: item[key] for key in ('id', 'run_id', 'conversation_id', 'project', 'title')}
    result.update(available=False, status='offline', status_label='连接暂不可用', nodes=0, completed=0)
    try:
        # The workbench checks the project/conversation binding and reads its
        # lightweight projection. The owner's full transcript can be very large.
        response = await client.get(item['url'] + '/api/workbench/view', params={
            key: item[key] for key in ('run_id', 'conversation_id', 'project')})
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError('Workflow response is too large')
        value = response.json()
        task = value.get('task') or {}
        if (task.get('mode') != 'workflow' or task.get('run_id') != item['run_id']
                or task.get('conversation_id') != item['conversation_id']):
            result['status_label'] = '运行连接已变化'
            return result
        nodes = (value.get('structure') or {}).get('nodes') or []
        result.update(available=True, status=task.get('execution_status', ''), status_label=LABELS.get(task.get('execution_status'), '已连接'),
                      url='/workbench/connections/' + item['id'], nodes=len(nodes),
                      completed=sum(n.get('status') in {'已完成', '已跳过'} for n in nodes))
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        pass
    return result


async def listing(*, profile=None, transport=None):
    entries = descriptors(profile)
    async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False, transport=transport) as client:
        return dict(connections=await asyncio.gather(*(inspect_connection(client, item) for item in entries)))
