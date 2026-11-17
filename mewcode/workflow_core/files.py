"""Immutable artifact snapshots and disposable Markdown delivery records."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tempfile


def capture(root, run_id, attempt_id, path, *, sandbox=None, kind='file'):
    source = Path(path).resolve(strict=True)
    if sandbox is not None and not sandbox.check(str(source), read_only=True)[0]:
        raise ValueError('没有读取此产物的权限')
    if not source.is_file():
        raise ValueError('产物不是文件')
    directory = Path(root) / run_id / 'workflow-artifacts' / attempt_id
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory)
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary)
        hasher = hashlib.sha256()
        with open(temporary, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(chunk)
        checksum = hasher.hexdigest()
        identity = hashlib.sha256((attempt_id + str(source) + checksum).encode()).hexdigest()[:32]
        destination = directory / (identity + source.suffix)
        os.replace(temporary, destination)
        return dict(id=identity, name=source.name, path=str(source), stored_path=str(destination),
                    sha256=checksum, size=destination.stat().st_size, kind=kind)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_report(root, run_id, attempt):
    directory = Path(root) / run_id / 'workflow-reports'
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (attempt['id'] + '.md')
    parts = [f'# {attempt["node_id"]} · 第 {attempt["number"]} 次执行', '']
    for progress in attempt.get('progress', []):
        parts.extend([progress['text'], ''])
    result = attempt.get('result') or {}
    final = str(result.get('text') or '')
    if final and (not attempt.get('progress') or attempt['progress'][-1]['text'] != final):
        parts.extend([final, ''])
    if result.get('reason'):
        parts.extend([result['reason'], ''])
    for artifact in attempt.get('artifacts', []):
        parts.append(f'- [{artifact.get("name", "产物")}]({artifact.get("path", "")})')
    text = '\n'.join(parts)
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, destination)
    return dict(id='report_' + attempt['id'], name=f'{attempt["node_id"]} · 交付记录.md',
                path=str(destination), stored_path=str(destination), kind='report',
                sha256=hashlib.sha256(text.encode()).hexdigest(), size=destination.stat().st_size)
