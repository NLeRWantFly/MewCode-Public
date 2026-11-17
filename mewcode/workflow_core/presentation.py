"""Model-authored, declarative input/output blocks bound to durable node sessions."""
from __future__ import annotations

import copy
import json
import math
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import dialogue
from .state import event


class DiagramNode(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=300)


class DiagramEdge(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: str
    target: str
    label: str = Field(default='', max_length=120)


class ChartSeries(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(max_length=120)
    values: list[float] = Field(max_length=100)


class PresentationBlock(BaseModel):
    model_config = ConfigDict(extra='forbid')
    block_id: str = Field(pattern=r'^[\w.:-]{1,80}$', description='Stable ID; reuse it to update this block')
    slot: Literal['input', 'output']
    kind: Literal['text', 'table', 'chart', 'diagram', 'image']
    title: str = Field(min_length=1, max_length=160)
    text: str = Field(default='', max_length=12000)
    columns: list[str] = Field(default_factory=list, max_length=20)
    rows: list[list[str | int | float | bool | None]] = Field(default_factory=list, max_length=100)
    chart_type: Literal['bar', 'line'] = 'bar'
    labels: list[str] = Field(default_factory=list, max_length=100)
    series: list[ChartSeries] = Field(default_factory=list, max_length=8)
    nodes: list[DiagramNode] = Field(default_factory=list, max_length=60)
    edges: list[DiagramEdge] = Field(default_factory=list, max_length=120)
    artifact_id: str = Field(default='', max_length=180)
    artifact_path: str = Field(default='', max_length=1600,
        description='Alternative: exact path of an image already registered in this run')
    caption: str = Field(default='', max_length=1000)

    @model_validator(mode='after')
    def validate_content(self):
        if self.kind == 'text' and not self.text.strip():
            raise ValueError('文字块需要 text')
        if self.kind == 'table' and (not self.columns or any(len(row) != len(self.columns) for row in self.rows)):
            raise ValueError('表格需要 columns，每行单元格数应与列数一致')
        if self.kind == 'chart' and (not self.labels or not self.series or any(
                len(row.values) != len(self.labels) or any(not math.isfinite(value) for value in row.values)
                for row in self.series)):
            raise ValueError('图表需要 labels 和等长、有限数值的 series')
        ids = [node.id for node in self.nodes]
        if self.kind == 'diagram' and (not ids or len(ids) != len(set(ids)) or any(
                edge.source not in ids or edge.target not in ids for edge in self.edges)):
            raise ValueError('关系图的节点 ID 必须唯一，连线端点必须存在')
        if self.kind == 'image' and not (self.artifact_id or self.artifact_path):
            raise ValueError('图片需要已登记产物的 artifact_id 或 artifact_path')
        try:
            encoded = json.dumps(self.model_dump(), ensure_ascii=False, allow_nan=False)
        except ValueError as exc:
            raise ValueError('表达块不能包含非有限数值') from exc
        if len(encoded) > 48000:
            raise ValueError('表达块过大，请用产物文件保存完整数据并提交紧凑表达')
        return self


def publish(state, identity, *, from_chat=False, **body):
    block = PresentationBlock.model_validate(body).model_dump()
    attempt = state['attempts'][identity]
    row = state['nodes'].get(attempt['node_id'], {})
    request = state.get('node_requests', {}).get(attempt.get('request_id'), {})
    if not from_chat and (attempt.get('status') != 'running' or (request and request.get('status') != 'running')):
        raise ValueError('这次执行已结束，不能更新表达块')
    if not request and (row.get('attempt_id') != identity or row.get('generation') != attempt.get('generation')):
        raise ValueError('这次执行已被新代次替代，原表达保留在历史记录中')
    if block['kind'] == 'image':
        normalized = lambda value: str(value or '').replace('\\','/').casefold()
        found = next((item for value in state['attempts'].values() for item in value.get('artifacts', [])
                      if (block['artifact_id'] and item.get('id') == block['artifact_id']) or (
                          not block['artifact_id'] and block['artifact_path'] and normalized(block['artifact_path']) in
                          {normalized(item.get(key)) for key in ('path','stored_path','logical_path')})), None)
        if not found or not str(found.get('name') or found.get('path', '')).lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
            raise ValueError('图片必须引用本次运行已登记的 PNG、JPEG 或 WebP 产物')
        block['artifact_id'] = found['id']
    session = dialogue.register(state, attempt)
    blocks = session.setdefault('presentation_blocks', {})
    key = block['slot'] + ':' + block['block_id']
    value = dict(block, node_id=attempt['node_id'], attempt_id=identity, session_id=session['id'],
                 order=blocks.get(key, {}).get('order', time.time_ns()), updated_at=time.time())
    if from_chat:
        value['author'] = 'chat'
    blocks[key] = copy.deepcopy(value)
    attempt.setdefault('presentation_blocks', {})[key] = copy.deepcopy(value)
    event(state, 'presentation_updated', node_id=attempt['node_id'], attempt_id=identity,
          block_id=block['block_id'], slot=block['slot'])
    return dict(block_id=block['block_id'], slot=block['slot'], kind=block['kind'], saved=True)


def visible(state, attempt):
    session = state.get('node_sessions', {}).get(attempt.get('session_id'), {})
    return copy.deepcopy(sorted((session.get('presentation_blocks') or {}).values(), key=lambda value: value['order']))
