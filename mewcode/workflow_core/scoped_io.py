"""Optional explicit material/output boundaries for a scoped workflow trial."""
from pathlib import Path

from mewcode.tools.base import Tool, ToolResult


class ScopedFileTool(Tool):
    def __init__(self, original, workspace, reads, writes):
        self.original, self.workspace = original, Path(workspace).resolve()
        self.reads, self.writes = tuple(Path(root).resolve() for root in reads), tuple(Path(root).resolve() for root in writes)
        for name in ('name', 'description', 'params_model', 'category', 'is_concurrency_safe',
                     'is_system_tool', 'should_defer', 'consumes_runtime_slot', 'inline_schema_refs'):
            setattr(self, name, getattr(original, name))

    def permission_risk(self, arguments):
        return self.original.permission_risk(arguments)

    async def execute(self, params):
        target = Path(getattr(params, 'file_path', '') or getattr(params, 'path', ''))
        if not target.is_absolute():
            target = self.workspace / target
        target = target.resolve()
        roots = self.writes if self.name in {'WriteFile', 'EditFile'} else self.reads
        if not any(target == root or target.is_relative_to(root) for root in roots):
            return ToolResult('该路径不属于本次指定材料或独立输出目录，请使用当前输入中绑定的路径。', is_error=True)
        return await self.original.execute(params)


def install(agent, config, artifact_root):
    scope = config.get('file_scope')
    if not scope:
        return
    reads = [*(scope.get('read_roots') or []), *(scope.get('write_roots') or []), str(artifact_root)]
    writes = scope.get('write_roots') or []
    for name in ('ReadFile', 'WriteFile', 'EditFile', 'ReportArtifact'):
        original = agent.registry.get(name)
        if original:
            discovered = agent.registry.is_discovered(name)
            agent.registry.register(ScopedFileTool(original, agent.work_dir, reads, writes))
            if discovered:
                agent.registry.mark_discovered(name)
