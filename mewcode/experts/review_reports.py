"""A loop reviewer may write its declared report, never its reviewed inputs."""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import re

from mewcode.tools.base import Tool, ToolResult, resolve_tool_path
from mewcode.tools.write_file import Params


def _path_label(value):
    # Contracts often attach a description after the literal relative path.
    raw = re.split(r'[\s：]', str(value).strip().strip('`'), maxsplit=1)[0].rstrip('`').replace('\\', '/')
    if (not raw or raw.startswith('/') or PureWindowsPath(raw).drive or ':' in raw
            or any(c in raw for c in '*?[]') or '\x00' in raw):
        return ''
    parts = raw.rstrip('/').split('/')
    if any(part in {'', '.', '..'} or part.casefold() in {'.git', '.codex', '.agents', '.mewcode'}
           or ('<' in part or '>' in part) and not re.fullmatch(r'<[A-Za-z_][A-Za-z0-9_]*>', part)
           for part in parts):
        return ''
    return '/'.join(parts)


def _matches(path, template, *, descendants=False):
    pattern = '/'.join('[^/<>:]+' if part.startswith('<') else re.escape(part)
                       for part in template.split('/'))
    if descendants:
        pattern += '(?:/.*)?'
    return re.fullmatch(pattern, path, flags=re.IGNORECASE if os.name == 'nt' else 0) is not None


def report_scope(contract):
    """Narrow explicit contract outputs; this never grants write authority."""
    outputs = tuple(dict.fromkeys(path for item in contract.get('deliverables', [])
        if (path := _path_label(item)) and Path(path).suffix))
    inputs = tuple(dict.fromkeys(path for item in contract.get('inputs', [])
                                if (path := _path_label(item))))
    # Unknown input scopes cannot safely be widened to report-writing access.
    if not outputs or any(not _path_label(item) for item in contract.get('inputs', [])):
        return None
    return {'outputs': outputs, 'inputs': inputs}


class WriteReviewReport(Tool):
    """Keep WriteFile's normal authorization and overwrite checks intact."""
    name = 'WriteFile'
    params_model = Params
    category = 'write'

    def __init__(self, delegate, *, work_dir, scope, sources, node_id=''):
        self.delegate = delegate
        self.root = Path(work_dir).resolve()
        self.scope = scope
        self.protected = []
        for source_key, source in (sources or {}).items():
            if not isinstance(source, dict):
                continue
            own_previous = bool(node_id and source_key == 'previous:' + node_id
                                and source.get('node_id') == source_key)
            for artifact in [*(source.get('fixed_artifacts') or []),
                             *((source.get('handoff') or {}).get('artifacts') or [])]:
                if not isinstance(artifact, dict):
                    continue
                for key in ('path', 'source_path', 'snapshot_path'):
                    if artifact.get(key):
                        # A prior delivery is a revision source, not an input
                        # owned by another node. Its frozen snapshot remains
                        # protected; only this node's declared live report may
                        # be replaced through the normal write authorization.
                        if own_previous and key != 'snapshot_path':
                            continue
                        self.protected.append((self.root / artifact[key]).resolve())
        self.description = (delegate.description + '\n循环复核仅可写本节点合同声明的报告路径：'
            + ', '.join(scope['outputs'])
            + '。被核验输入和其他路径禁止写入。已有完整报告正文直接复用，落盘并回读后提交 SubmitHandoff；'
              'SubmitLoopDecision 不代替文件交付。')

    async def execute(self, params):
        try:
            target = resolve_tool_path(params.file_path).resolve()
            relative = target.relative_to(self.root).as_posix()
            allowed = any(_matches(relative, path) for path in self.scope['outputs'])
            denied = (any(_matches(relative, path, descendants=True) for path in self.scope['inputs'])
                      or any(target == path or target.is_relative_to(path) for path in self.protected)
                      or any(part.casefold() in {'.git', '.codex', '.agents', '.mewcode'}
                             for part in Path(relative).parts)
                      or (target.exists() and target.stat().st_nlink > 1))
        except (OSError, ValueError, RuntimeError):
            allowed, denied = False, True
        if not allowed or denied:
            return ToolResult(output='循环复核只能写合同声明的本节点报告，不能修改被核验输入或其他路径。',
                              is_error=True, host_observation={'operation_blocked': True, 'phase': 'before_effect'})
        return await self.delegate.execute(params)
