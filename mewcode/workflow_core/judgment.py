"""Decode explicit decisions; formatting feedback stays inside the node."""
from __future__ import annotations

import json


def instructions(node):
    text = ('最终业务判断只输出 JSON：{"value": true 或 false 或命名结果, "reason": "实际判断理由"}。'
            '不要用对话摘要或输出格式示例代替实际判断。')
    if node['config'].get('legacy_loop_decision'):
        text += '本循环的 EXIT 对应 value:true，CONTINUE 对应 value:false。'
    return text


def parse(text, node):
    text = str(text).strip()
    if text.startswith('```'):
        lines = text.splitlines()
        if len(lines) < 3 or lines[-1].strip() != '```':
            raise ValueError('判断 JSON 代码块未闭合')
        text = '\n'.join(lines[1:-1])
    decision = json.loads(text)
    if (not isinstance(decision, dict) or 'value' not in decision
            or not isinstance(decision['value'], (bool, str))
            or isinstance(decision['value'], str) and not decision['value'].strip()):
        raise ValueError('判断结果需要布尔值或非空命名结果')
    value = decision['value']
    if node['config'].get('legacy_loop_decision') and isinstance(value, str):
        values = {'EXIT': True, 'CONTINUE': False}
        if value.strip().upper() not in values:
            raise ValueError('循环判断需要 true/false 或 EXIT/CONTINUE')
        value = values[value.strip().upper()]
    return dict(value=value, text=str(decision.get('reason') or text))


def repair_message(node, error):
    return ('上一条最终回复未能读取为业务判断：' + str(error) + '。这是节点内部的格式反馈，'
            '请接续当前对话自行修正，不请求用户解除格式阻塞。保留已有成果和工具回执，'
            '不要重复已完成的业务。若上一条只是压缩摘要、尚未完成实际检查，请继续未完成部分，'
            '仍可使用当前工具；摘要服务的“只输出摘要、不能调用工具”不代表用户暂停任务。'
            '不得从历史摘要、示例或未核实信息中猜测判断值。\n' + instructions(node))
