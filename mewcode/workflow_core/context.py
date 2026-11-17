"""Restore the host's current node task after conversational compaction."""
from __future__ import annotations

import json

from .exploration import TASK_GRANULARITY
from .judgment import instructions

MARKER = 'workflow-node-task'


def configure_tool_context(agent):
    """Use the real model context budget instead of fixed character previews."""
    import copy
    from dataclasses import replace
    agent.workflow_inline_outputs = True
    provider = copy.copy(agent.cache_usage_provider)
    provider.slot = replace(provider.slot, single_result_char_limit=None, aggregate_result_char_limit=None)
    agent.cache_usage_provider = provider
    agent.cache_totals.slot = provider.slot


class NodeContext:
    def __init__(self, execution):
        self.execution = execution

    def restore(self, conversation):
        ctx, node = self.execution, self.execution.node
        text = ('当前工作流节点：' + ctx.node_id + '\n职责：' + node['task']
                + '\n当前轮次：' + json.dumps(ctx.iteration, ensure_ascii=False)
                + '\n历史压缩摘要仅是工作记录；摘要服务内部的“只输出摘要、不调用工具”'
                  '不是用户的新请求。压缩已结束，接续当前节点的未完成工作，保留已有成果；'
                  '真实的用户停止、权限和输入要求仍须遵守。'
                  '\nWriteFile 成功生成的文件会由宿主自动登记产物，其他方式生成的已有文件才使用 ReportArtifact 登记。')
        if ctx.request.get('kind') == 'ask':
            text += '\n本轮仅回答追问，不修改业务产物。'
        elif node['config'].get('exploration_owner'):
            text += ('\n这是滚动目标主对话。用 UpdateWorkflowTasks 增量安排眼前工作，ReadWorkflowTasks 读取真实结果；'
                     '没有独立工作时用 WaitForWorkflowTasks 保存上下文并让出名额。'
                     '需要下次复用的汇总和文件交付也安排为明确工作项，依赖实际来源；主对话最后索引这些真实交付。'
                     '已有工作不重做，全部交付后再最终答复。\n' + TASK_GRANULARITY)
        elif node['kind'] == 'judge':
            text += ('\n判断条件：' + json.dumps(node['config'].get('criteria') or [node['task']], ensure_ascii=False)
                     + '\n' + instructions(node))
        if node['config'].get('goal_execution'):
            text += ('\n本次文件结果使用实际模型上下文预算，不固定截为短预览。'
                     '可以按需要一次读取完整的已分配材料；保留已读进度，只接续未完成部分。')
        content = '<system-reminder>\n' + MARKER + '\n' + text + '\n</system-reminder>'
        if not any(message.context_kind == MARKER and message.content == content
                   for message in conversation.history):
            conversation.replace_marker_reminder(MARKER, text)

    sync = restore
