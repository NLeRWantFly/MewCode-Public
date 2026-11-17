"""Durable node-to-primary assistance, consumed at a settled tool boundary."""
from __future__ import annotations

import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from mewcode.tools.base import Tool, ToolResult


class AssistanceScopeRequest(BaseModel):
    """Expose the existing authorization contract to the model, not just a dict."""

    model_config = ConfigDict(extra='allow')
    operation_class: Literal['external_send', 'network', 'write_outside_workspace', 'install_dependency',
                             'run_command_prefix', 'spend_over_cap', 'other']
    scope: dict = Field(min_length=1, description=(
        '具体范围对象，不允许通配符。可用字符串数组字段：operation_classes、tools、paths、hosts、'
        'model_routes、content_classes、node_ids、command_prefix；非负整数字段：cost_cap_tokens、epoch、round；'
        '非空字符串字段：case_id、operation_digest。external_send 必须有 model_routes 和 content_classes；'
        'network 必须有 hosts；write_outside_workspace 必须有 paths；run_command_prefix 必须有 command_prefix。'
        '例如只读范围：{"tools":["ReadFile"],"paths":["具体配置路径"]}，operation_class 使用 other。'))

    @model_validator(mode='after')
    def supported_scope(self):
        from mewcode.experts.authorization_ledger import valid_request
        valid_request(self.model_dump())
        return self


class RequestAssistanceParams(BaseModel):
    kind: Literal['capability', 'environment', 'effect_verification', 'permission', 'diagnosis', 'investigation'] = Field(
        description='capability=缺少工具；environment=运行环境故障；effect_verification=操作效果未知；permission=授权缺口；diagnosis=原因诊断；investigation=请求具体范围的隔离调查。')
    need: str = Field(min_length=1, max_length=4000,
        description='说明本节点目标、已完成工作、具体障碍、已尝试步骤和希望协助者确认的事项。不要只写“继续”或“帮忙”。')
    evidence_refs: list[str] = Field(default_factory=list, max_length=64,
        description='实际工具调用 ID、账本事实 ID 或已知产物路径；引用已有证据，不编造成功回执。')
    requested_tools: list[str] = Field(default_factory=list, max_length=64,
        description='需要协助者使用的准确工具名；这里只是需求，不会注册工具或授予权限。调查时须包含在 scope_requests 的具体范围中。')
    scope_requests: list[AssistanceScopeRequest] = Field(default_factory=list, max_length=8,
        description='仅请求新增调查授权时填写具体操作与范围；kind=investigation 时至少一项。普通能力诊断使用 capability/diagnosis，无需虚构提权。')

    @model_validator(mode='after')
    def investigation_scope(self):
        if self.kind == 'investigation' and not self.scope_requests:
            raise ValueError('调查提权须列明具体工具、路径、网络或外发范围')
        return self


class RequestAssistance(Tool):
    name = 'RequestAssistance'
    category = 'read'
    is_system_tool = True
    consumes_runtime_slot = False
    params_model = RequestAssistanceParams
    description = (
        '场景：编排节点无法自行解决具体能力、环境、权限或操作效果问题时，向主模型请求协助。'
        '输入：kind、具体 need，可附已有 evidence_refs 和 requested_tools。普通只读排查选 diagnosis/capability；'
        '只有请求隔离调查时选 investigation，且 scope_requests 至少含一项 operation_class 与 scope。'
        '例：{"kind":"diagnosis","need":"读取上游服务配置后仍无法确定连接失败原因，请核对错误记录",'
        '"requested_tools":["ReadFile"]}。'
        '成功返回 request_id、status=pending；宿主在本次工具轮结束后让出节点并安排协助，回执返回后接续。'
        '收到 pending 后停止继续操作，不再用 completed 交接，也不要轮询或重复请求。'
        '参数错误时按错误修正；已有不同 pending 请求时等待该请求结果。仅限活动编排节点。'
        '请求不会自动增加工具、授权、预算或把业务标为完成；无关需求澄清不使用本工具。')

    def __init__(self, store=None, run_id='', node_id=''):
        self.store, self.run_id, self.node_id = store, run_id, node_id

    async def execute(self, params):
        if self.store is None:
            return ToolResult(output='Assistance requires an active orchestration node.', is_error=True)
        from mewcode.experts.node_ledger import ledger_for_run
        from mewcode.experts.recovery_state import digest
        request = params.model_dump()
        def save(record):
            ledger = ledger_for_run(record)
            view = ledger.view(self.node_id)
            request.update(node_id=self.node_id, epoch=view.epoch, origin='node')
            request['request_id'] = digest(request)
            stage = record['node_states'][self.node_id]
            old = stage.get('assistance_request') or {}
            if old.get('status') == 'pending' and old.get('request_id') != request['request_id']:
                raise ValueError('The current assistance request is still pending')
            stage['assistance_request'] = {**request, 'status': 'pending'}
            ledger.record(self.node_id, 'assistance', {**request, 'status': 'requested'}, source='node')
        try:
            self.store.mutate(self.run_id, save, durable=True)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=json.dumps({'request_id': request['request_id'], 'status': 'pending',
            'next_step': '保存进展，等待主模型协助回执。'}, ensure_ascii=False))


class EffectVerification(BaseModel):
    request_id: str = Field(description='效果未知的原操作 request_id，不是当前协助请求 ID。')
    effect: Literal['applied', 'not_applied', 'partial']
    evidence_refs: list[str] = Field(min_length=1, max_length=32,
        description='本次实际读取目标文件或状态的工具调用 ID；宿主核验后才认定操作效果。')


class SubmitAssistanceParams(BaseModel):
    next_step: Literal['resume', 'rebase', 'ask_human', 'fail'] = Field(
        description='resume=返回原节点继续核验；rebase=提交受限执行方案修订；ask_human=需要具体授权/业务决定；fail=无法恢复并说明原因。')
    notes: str = Field(max_length=8000, description='调查发现、实际证据、尚未解决事项，以及原节点下一步。不能用推测宣告障碍已解除。')
    artifacts: list[str] = Field(default_factory=list, description='协助产生或实际核验的产物路径；无文件时留空。')
    clearances: list[dict] = Field(default_factory=list,
        description='可选宿主事实引用，每项含 blocker_fact_id、evidence_ref、clearance_kind。仅引用本次请求和当前 epoch 的真实宿主记录；无此记录留空，不能自造放行结论。')
    patch: dict | None = Field(default=None, description='仅 rebase 使用；nodes 列表中的受限节点修订，格式和不可修改项见工具说明。')
    effect_verifications: list[EffectVerification] = Field(default_factory=list)
    authority_needed: list[AssistanceScopeRequest] = Field(default_factory=list, max_length=16)


class SubmitAssistanceReceipt(Tool):
    name = 'SubmitAssistanceReceipt'
    category = 'read'
    is_system_tool = True
    consumes_runtime_slot = False
    params_model = SubmitAssistanceParams
    description = (
        '场景：仅宿主派发的协助节点提交诊断回执，普通业务节点使用 RequestAssistance 请求帮助。'
        '必须填写 next_step 和 notes；例如 {"next_step":"resume","notes":"已核对输入路径，原节点可按实际路径继续"}。'
        '成功只表示回执已收集、待宿主核验；随后仍须 SubmitHandoff 保存本段结论。'
        'clearances 引用本次宿主核验事实；宿主逐项校验后回调原节点。无证据时不填写 clearances。'
        '更换节点执行方法用 next_step=rebase，patch 格式为 '
        '{"nodes":[{"node_id":"原节点ID","responsibility":"修订职责","tools":["ReadFile"],'
        '"deny_tools":[],"failure_policy":{"action":"retry","max_attempts":3}}]}。'
        '只填写需要修改的字段；仅限当前节点和未开始的下游（含其循环成员）。'
        '保留显式工具禁用、模型、预算、验收、门禁与循环合同；未核验操作效果或原问题仍阻止接纳。'
        '接纳修订后须回到原节点执行并核验，提交本回执不代表业务完成。')

    def __init__(self, holder):
        self.holder = holder

    async def execute(self, params):
        self.holder.clear()
        self.holder.update(params.model_dump())
        return ToolResult(output='Assistance receipt submitted for host verification.')
