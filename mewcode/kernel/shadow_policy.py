"""Preserve source permissions while restricting physical writes to a snapshot."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import os

from mewcode.permissions.checker import Decision


def command_in_scope(command, requests):
    from mewcode.permissions.command_rules import parse_command
    parsed = parse_command(command, 'powershell' if os.name == 'nt' else 'bash')
    prefixes = [tuple(request['scope']['command_prefix']) for request in requests
                if request['operation_class'] == 'run_command_prefix']
    return (parsed.safe_for_prefix_rules and len(parsed.segments) == 1 and not parsed.segments[0].redirections
            and any(parsed.segments[0].arguments[:len(prefix)] == prefix for prefix in prefixes))


def bind_collaboration_worktree(checker, parent, workspace):
    """Check logical source grants as well as the physical isolated scope."""
    source = Path(workspace['source']).resolve(strict=True)
    isolated = Path(workspace['path']).resolve(strict=True)
    archive = source / '.mewcode' / 'collaboration-worktrees'
    if (not isolated.is_relative_to(archive) or isolated != Path(workspace['path'])
            or isolated.parent.parent != archive or isolated.name != workspace['name']
            or workspace.get('status') not in {'active', 'sealed'} or parent is None):
        raise ValueError('隔离工作区缺少有效宿主归属')
    original = checker.check
    def check(tool, arguments):
        mapped = dict(arguments)
        if tool.name in {'ReadFile', 'WriteFile', 'EditFile', 'ReportArtifact'}:
            target = Path(str(arguments.get('file_path') or ''))
            target = (target if target.is_absolute() else isolated / target).resolve()
            if target.is_relative_to(isolated):
                relative = target.relative_to(isolated)
                if tool.category != 'read' and any(part.casefold() in {'.git', '.mewcode'} for part in relative.parts):
                    return Decision('deny', '节点不能修改宿主 Git 与工作区控制记录。', source='worktree_scope')
                mapped['file_path'] = str(source / relative)
            elif tool.category != 'read':
                return Decision('deny', '节点写入不能离开当前 worktree。', source='worktree_scope')
        inherited = parent.check(tool, mapped)
        if inherited.effect in {'deny', 'ask'}:
            return inherited
        child = original(tool, arguments)
        return inherited if inherited.effect == 'review' and child.effect == 'allow' else child
    checker.check = check
    authorization = getattr(parent, 'task_authorization', None)
    if authorization is not None:
        writes = []
        for raw in authorization.write_directories:
            allowed = Path(raw).resolve()
            if source.is_relative_to(allowed):
                writes.append(str(isolated))
            elif allowed.is_relative_to(source):
                writes.append(str(isolated / allowed.relative_to(source)))
        narrowed = replace(authorization, write_directories=tuple(sorted(set(writes))),
                           allowed_behaviors=tuple(value for value in authorization.allowed_behaviors if value != 'git_write'))
        fields = [narrowed.task_id, narrowed.read_directories, narrowed.write_directories, narrowed.network_hosts,
                  narrowed.allowed_tools, narrowed.allowed_behaviors, narrowed.expires_at]
        narrowed = replace(narrowed, version=hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()[:16])
        checker.set_task_authorization(narrowed, task_id=narrowed.task_id)
    checker.requires_command_isolation = True


def bind_collaboration_candidate(checker, parent, context):
    """Map a host-created candidate back to the original permission scope."""
    source = Path(context['source']).resolve(strict=True)
    isolated = Path(context['candidate']).resolve(strict=True)
    root = isolated.parent
    archive = source / '.mewcode' / 'collaboration-candidates'
    if (root.parent != archive or not root.name.startswith('candidate-') or isolated.name != 'candidate'
            or archive.resolve() != archive or Path(context['baseline']).resolve() != root / 'baseline'):
        raise ValueError('候选目录不是当前项目的宿主事务')
    record = json.loads((root / 'transaction.json').read_text(encoding='utf-8'))
    if (Path(record['source']).resolve() != source or Path(record['candidate']).resolve() != isolated
            or record['state'] not in {'active', 'reviewing'}):
        raise ValueError('候选事务已变化，不能派发工具')
    if parent is None:
        raise ValueError('候选执行缺少原工作区权限上下文')
    original = checker.check
    def check(tool, arguments):
        mapped = dict(arguments)
        if tool.name in {'ReadFile', 'WriteFile', 'EditFile', 'ReportArtifact'}:
            path = Path(str(arguments.get('file_path') or ''))
            path = (path if path.is_absolute() else isolated / path).resolve()
            if path.is_relative_to(isolated):
                relative = path.relative_to(isolated)
                if tool.category != 'read' and any(part.casefold() in {'.venv', 'venv'} for part in relative.parts):
                    return Decision('deny', '候选冻结的 Python 依赖只读，不能作为交付物修改。', source='candidate_runtime')
                mapped['file_path'] = str(source / relative)
            elif tool.category != 'read':
                return Decision('deny', '候选写入不能离开隔离目录。', source='candidate_scope')
        source_decision = parent.check(tool, mapped)
        if source_decision.effect in {'deny', 'ask'}:
            return source_decision
        child_decision = original(tool, arguments)
        return source_decision if source_decision.effect == 'review' and child_decision.effect == 'allow' else child_decision
    checker.check = check
    # The parent checker still assesses original paths and grants on every call.
    # Native commands cannot use this remapping to write outside the candidate.
    checker.requires_command_isolation = True


def bind_shadow_checker(checker, parent, snapshot):
    source = Path(snapshot['workspace']).resolve(strict=True)
    isolated = Path(snapshot['work_dir']).resolve(strict=True)
    expected = source / '.mewcode' / 'snapshot-worktrees' / ('snapshot-' + snapshot['snapshot_id'])
    if isolated != expected.resolve(strict=True):
        raise ValueError('investigation workspace does not match its host snapshot')
    original = checker.check
    def check(tool, arguments):
        mapped = dict(arguments)
        if tool.name == 'Bash':
            if not command_in_scope(str(arguments.get('command') or ''), snapshot.get('scope_requests', [])):
                return Decision('deny', '调查命令不在所声明的明确命令范围中。', source='snapshot_command_scope')
        if tool.name in {'ReadFile', 'WriteFile', 'EditFile', 'ReportArtifact'}:
            path = Path(str(arguments.get('file_path') or ''))
            path = path if path.is_absolute() else isolated / path
            try:
                relative = path.resolve().relative_to(isolated)
            except ValueError:
                if tool.category != 'read':
                    return Decision('deny', '调查写入只能发生在当前快照中。', source='snapshot_scope')
            else:
                mapped['file_path'] = str(source / relative)
        source_decision = parent.check(tool, mapped)
        if source_decision.effect in {'deny', 'ask'}:
            return source_decision
        child_decision = original(tool, arguments)
        return source_decision if source_decision.effect == 'review' and child_decision.effect == 'allow' else child_decision
    checker.check = check
    authorization = getattr(parent, 'task_authorization', None)
    if authorization is not None:
        def map_roots(roots):
            result = []
            for value in roots:
                path = Path(value).resolve()
                if source.is_relative_to(path):
                    result.append(str(isolated))
                elif path.is_relative_to(source):
                    result.append(str(isolated / path.relative_to(source)))
            return tuple(sorted(set(result)))
        narrowed = replace(authorization, write_directories=map_roots(authorization.write_directories),
                           allowed_behaviors=tuple(value for value in authorization.allowed_behaviors if value != 'git_write'))
        fields = [narrowed.task_id, narrowed.read_directories, narrowed.write_directories, narrowed.network_hosts,
                  narrowed.allowed_tools, narrowed.allowed_behaviors, narrowed.expires_at]
        narrowed = replace(narrowed, version=hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()[:16])
        checker.set_task_authorization(narrowed, task_id=narrowed.task_id)
    # The normal inherited checker/native executor still enforces isolation,
    # network scope, hard denies, sensitive paths and per-call safety review.
    checker.requires_command_isolation = True
