"""Pipeline REST API 路由。

提供 Pipeline 的 CRUD 和执行接口。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Coroutine, Mapping
from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from mewcode.pipeline.assets import AssetError, AssetStore
from mewcode.pipeline.actions import PipelineAction, normalize_actions
from mewcode.pipeline.compiler import PipelineCompiler
from mewcode.pipeline.executor import PipelineExecutor
from mewcode.pipeline.models import (
    CreatePipelineRequest,
    PipelineGraph,
    PipelineListItem,
    PipelineRunRecord,
    RunPipelineRequest,
    UpdatePipelineRequest,
    ValidateResponse,
)
from mewcode.pipeline.models import NodeStatus, NodeType, RunStatus
from mewcode.pipeline.next_run import (
    build_next_run_prompt,
    decide_by_rule,
    parse_next_run_reply,
)
from mewcode.pipeline.schedule import (
    PipelineScheduler,
    PipelineScheduleStore,
    PipelineTrigger,
    compute_next_fire,
    compute_next_fires,
)
from mewcode.pipeline.schedule_resolver import (
    ResolvedSchedule,
    build_llm_prompt,
    describe_cron,
    parse_llm_reply,
    resolve_schedule_text,
)
from mewcode.pipeline.schedule_review import (
    ScheduleReviewDecision,
    build_schedule_review_prompt,
    parse_schedule_review_reply,
    reconcile_ephemeral_review,
    structural_review,
)
from mewcode.pipeline.store import PipelineStore, VersionConflictError
from mewcode.plugins.fastapi import FastAPIInstallResult

log = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])

# 全局存储实例（在 register_pipeline_api 中初始化）
_store: PipelineStore | None = None
_asset_store: AssetStore | None = None
# 定时触发注册存储与后台调度循环（同上）
_schedule_store: PipelineScheduleStore | None = None
_scheduler: PipelineScheduler | None = None
# 执行器构造参数（每个 run 实例化一个独立 executor，避免共享可变状态）
_executor_kwargs: dict[str, Any] = {}
# 能力枚举提供者（宿主注入的异步函数；None 时 capabilities 除 modes 外均为空列表）
_capabilities_provider: Callable[[], Coroutine[Any, Any, dict]] | None = None

# 事件缓冲上限（重连补发用；thinking 增量不入缓冲）
_EVENT_BUFFER_CAP = 8000
# 已结束 run 的句柄保留数量
_DONE_HANDLE_CAP = 30


class _RunHandle:
    """一次后台执行的运行时句柄：事件缓冲 + 订阅者 + 任务引用。

    run 的生命周期与任何 HTTP/SSE 连接无关——断线只是少了一个订阅者，
    重连后从缓冲区补发（配合 SSE 的 Last-Event-ID）。
    """

    def __init__(self, run_id: str, graph_id: str) -> None:
        self.run_id = run_id
        self.graph_id = graph_id
        self.entry_node_id: str | None = None
        self.task: asyncio.Task | None = None
        self.executor: PipelineExecutor | None = None
        self.record: PipelineRunRecord | None = None
        self.events: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.done = asyncio.Event()
        self._seq = 0

    def emit(self, ev: dict) -> None:
        """接收 executor 事件：编号、入缓冲、分发给订阅者。"""
        self._seq += 1
        ev = dict(ev)
        ev["_seq"] = self._seq
        # thinking 增量体量大且纯装饰性，不进重连缓冲
        if ev.get("type") != "thinking":
            self.events.append(ev)
            if len(self.events) > _EVENT_BUFFER_CAP:
                del self.events[: len(self.events) - _EVENT_BUFFER_CAP]
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass  # 慢订阅者丢事件，重连时可从缓冲补发

    def finish(self) -> None:
        """标记完成并唤醒所有订阅者。"""
        self.done.set()
        sentinel = {"_seq": -1, "type": "_done"}
        for q in list(self.subscribers):
            try:
                q.put_nowait(sentinel)
            except asyncio.QueueFull:
                pass

    def current_record(self) -> PipelineRunRecord | None:
        if self.record is not None:
            return self.record
        if self.executor is not None:
            return self.executor.current_run_record
        return None


_active_runs: dict[str, _RunHandle] = {}
_adaptive_tasks: set[asyncio.Task] = set()


def _prune_handles() -> None:
    """限制已结束句柄的保留数量（按结束先后淘汰）。"""
    done = [h for h in _active_runs.values() if h.done.is_set()]
    if len(done) <= _DONE_HANDLE_CAP:
        return
    done.sort(key=lambda h: (h.record.completed_at or "") if h.record else "")
    for h in done[: len(done) - _DONE_HANDLE_CAP]:
        _active_runs.pop(h.run_id, None)


def register_pipeline_api(
    app: Any,
    work_dir: str,
    *,
    agent_factory: Callable[..., Coroutine[Any, Any, tuple[str, dict | None]]] | None = None,
    tool_executor: Callable[[str, dict], Coroutine[Any, Any, str]] | None = None,
    skill_executor: Callable[[str, dict], Coroutine[Any, Any, str]] | None = None,
    action_provider: Callable[[], Mapping[str, PipelineAction]] | None = None,
    capabilities_provider: Callable[[], Coroutine[Any, Any, dict]] | None = None,
) -> FastAPIInstallResult:
    """注册 Pipeline API 到 FastAPI 应用。

    Args:
        app: FastAPI 应用实例。
        work_dir: 工作目录。
        agent_factory: Agent 调用工厂函数。
        tool_executor: 工具执行函数。
        skill_executor: Skill 执行函数。
        action_provider: Cordis Pipeline Action 动态目录。
        capabilities_provider: 能力枚举提供者（异步返回
            {"models": [...], "tools": [...], "skills": [...],
             "actions": [...], "modes": [...]}
            的部分或全部键），供 GET /capabilities 使用。
    """
    global _store, _asset_store, _capabilities_provider, _schedule_store, _scheduler

    _store = PipelineStore(work_dir)
    _capabilities_provider = capabilities_provider
    _asset_store = AssetStore(work_dir)
    # 换 provider 后能力上下文缓存立即失效
    _model_efforts_cache["ts"] = 0.0
    _model_efforts_cache["value"] = None

    # 定时触发：从已保存的图重建注册（图是事实来源，store 只保留
    # 触发统计），调度循环随宿主应用的生命周期启停
    _schedule_store = PipelineScheduleStore(work_dir)
    for item in _store.list_pipelines():
        g = _store.get_pipeline(item.graph_id)
        if g is not None:
            _schedule_store.sync_graph(g)
    recovered_waits = _schedule_store.recover_in_flight()
    if recovered_waits:
        log.info(
            "[pipeline-schedule] recovered %d interrupted reviewed run(s)",
            recovered_waits,
        )
    recovered = _schedule_store.recover_pending_adaptive()
    if recovered:
        log.info("[pipeline-schedule] recovered %d adaptive trigger(s)", recovered)
    scheduler = PipelineScheduler(_schedule_store, on_fire=_fire_trigger)
    _scheduler = scheduler
    try:
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def pipeline_lifespan(app_instance: Any):
            async with original_lifespan(app_instance):
                await scheduler.start()
                try:
                    yield
                finally:
                    await scheduler.shutdown()

        app.router.lifespan_context = pipeline_lifespan
    except AttributeError:
        log.warning("App does not expose a lifespan context; scheduler not started")

    _executor_kwargs.clear()
    _executor_kwargs.update(
        agent_factory=agent_factory,
        tool_executor=tool_executor,
        skill_executor=skill_executor,
        action_provider=action_provider,
        # 三级回退决策的能力上下文（与 GET /capabilities 同源）与
        # pipeline 自动挡档位配置
        capabilities_provider=capabilities_provider,
        config=_load_app_config(),
        asset_resolver=_asset_store.content_block,
        artifact_writer=_store.save_run_artifact,
        artifact_dir=(
            Path(work_dir) / ".mewcode" / "pipelines" / "runs" / "artifacts"
        ),
    )

    app.include_router(router)
    log.info("Pipeline API registered")

    async def cleanup_pipeline() -> None:
        global _store, _asset_store, _capabilities_provider, _schedule_store, _scheduler

        await scheduler.shutdown()
        tasks = [
            handle.task
            for handle in _active_runs.values()
            if handle.task is not None and not handle.task.done()
        ]
        tasks.extend(task for task in _adaptive_tasks if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        _active_runs.clear()
        _adaptive_tasks.clear()
        if _scheduler is scheduler:
            _scheduler = None
            _store = None
            _asset_store = None
            _capabilities_provider = None
            _schedule_store = None
            _executor_kwargs.clear()

    return FastAPIInstallResult(
        service={
            "store": _store,
            "asset_store": _asset_store,
            "schedule_store": _schedule_store,
            "scheduler": scheduler,
        },
        cleanup=cleanup_pipeline,
    )


def _load_app_config() -> Any:
    """加载应用配置（pipeline 自动挡档位 auto_heavy_model/auto_light_model 用）。

    失败降级 None：auto_policy 走内置分档表 / 会话默认模型兜底。
    """
    try:
        from mewcode.config import load_config

        return load_config()
    except Exception as e:
        log.debug("load_config failed, pipeline tier config unavailable: %s", e)
        return None


def _make_executor() -> PipelineExecutor:
    """为一次 run 创建独立的 executor 实例。

    单例 executor 的 on_event 会被并发 run 互相覆盖；每 run 一个实例
    彻底消除共享可变状态（含 auto_policy 细判缓存）。

    档位配置（pipeline.auto_heavy_model/auto_light_model）每次 run 现读，
    改 .mewcode/config.yaml 后无需重启即生效；读取失败沿用注册时的快照。
    """
    kwargs = dict(_executor_kwargs)
    fresh_config = _load_app_config()
    if fresh_config is not None:
        kwargs["config"] = fresh_config
    return PipelineExecutor(
        **kwargs,
        on_checkpoint=(
            (lambda rec: _store.save_run_snapshot(rec)) if _store is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# 请求模型（仅 API 层使用，不进 models.py）
# ---------------------------------------------------------------------------


class UpdatePipelineBody(UpdatePipelineRequest):
    """PUT 请求体：UpdatePipelineRequest + 乐观锁版本号。"""

    version: int | None = None
    """乐观锁：客户端加载图时的 version。保存前与存储中的当前版本比对，
    不一致返回 409（含 current_version）；None = 跳过比对（旧客户端兼容）。"""


class InlineRunRequest(BaseModel):
    """内联试跑请求：图 JSON 直接随 body 传入，不落盘管线本体。"""

    graph: dict[str, Any]
    """完整图 JSON（与保存/导出的 PipelineGraph 结构一致）。"""
    trigger_input: dict[str, Any] = Field(default_factory=dict)
    budget_total: int | None = None
    """token 预算上限。"""
    entry_node_id: str | None = None
    """入口节点 ID：None = 从 start 整图；指定时从该节点向后试跑子图。"""


# ---------------------------------------------------------------------------
# 校验/序列化辅助
# ---------------------------------------------------------------------------

# 能力上下文缓存：model -> 可用 effort 档位（capabilities provider 无缓存，
# 保存/校验高频调用时避免反复打 list_models）
_MODEL_EFFORTS_TTL = 30.0
_model_efforts_cache: dict[str, Any] = {"ts": 0.0, "value": None}


async def _get_model_efforts() -> dict[str, list[str]] | None:
    """从 capabilities provider 构建 model -> efforts 能力上下文（TTL 缓存）。

    与 GET /capabilities 的 models[].efforts 同源；provider 缺失/异常/
    无模型数据时返回 None（compiler.validate 据此跳过 effort-模型匹配校验）。
    """
    now = time.monotonic()
    if now - _model_efforts_cache["ts"] < _MODEL_EFFORTS_TTL:
        return _model_efforts_cache["value"]

    value: dict[str, list[str]] | None = None
    if _capabilities_provider is not None:
        try:
            data = await _capabilities_provider()
        except Exception:
            log.exception("Capabilities provider failed (model efforts)")
            data = None
        models = data.get("models") if isinstance(data, dict) else None
        if isinstance(models, list):
            parsed: dict[str, list[str]] = {}
            for item in models:
                if isinstance(item, dict) and item.get("id"):
                    efforts = item.get("efforts")
                    parsed[str(item["id"])] = (
                        [str(e) for e in efforts]
                        if isinstance(efforts, list)
                        else []
                    )
            value = parsed or None

    _model_efforts_cache["ts"] = now
    _model_efforts_cache["value"] = value
    return value


async def _get_model_vision() -> dict[str, bool] | None:
    if _capabilities_provider is None:
        return None
    try:
        data = await _capabilities_provider()
    except Exception:
        log.exception("Capabilities provider failed (vision)")
        return None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    parsed = {
        str(item["id"]): bool(item.get("vision", False))
        for item in models
        if isinstance(item, dict) and item.get("id")
    }
    return parsed or None


def _parse_graph(data: Any) -> PipelineGraph:
    """把 body 里的图 JSON 解析为 PipelineGraph，失败返回 400。"""
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Graph must be a JSON object")
    payload = dict(data)
    # 画布草稿可能还没起名；name 是必填字段，兜底后不阻断校验/试跑
    if not payload.get("name"):
        payload["name"] = "draft"
    try:
        return PipelineGraph.model_validate(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid graph: {e}") from None


def _dump_graph(graph: PipelineGraph) -> dict[str, Any]:
    """按 API 响应口径序列化图（边用 from/to 别名，与 response_model 行为一致）。"""
    return graph.model_dump(mode="json", by_alias=True)


def _validation_failed(result: ValidateResponse) -> JSONResponse:
    """校验 errors 阻断保存/试跑时的统一 400 响应。"""
    return JSONResponse(
        status_code=400,
        content={
            "detail": "Graph validation failed",
            "errors": result.errors,
            "warnings": result.warnings,
        },
    )


async def _auto_resolve_schedules(graph: PipelineGraph) -> None:
    """保存/校验前自动解析未决排期：规则优先，Agent 兜底。"""
    for node in graph.nodes:
        if node.type != NodeType.SCHEDULE or node.schedule is None:
            continue
        cfg = node.schedule
        if (
            cfg.resolved_cron
            or cfg.resolved_at
            or cfg.resolved_interval_seconds
            or not cfg.schedule_text
        ):
            continue
        resolved = await _resolve_schedule_async(cfg.schedule_text)
        if resolved is not None:
            cfg.resolved_cron = resolved.cron
            cfg.resolved_at = resolved.run_at
            cfg.resolved_interval_seconds = resolved.interval_seconds
            cfg.resolved_max_runs = resolved.max_runs
            log.info(
                "[pipeline-schedule] auto-resolved node %s: %r -> %s",
                node.id, cfg.schedule_text, resolved.cron or resolved.run_at,
            )


async def _validate_graph(graph: PipelineGraph) -> ValidateResponse:
    """compiler 静态校验 + 部署环境校验（tool/skill 直连执行器接线情况）。

    tool/skill 直连节点依赖 register_pipeline_api 注入的 tool_executor /
    skill_executor；本部署未接线时，含此类节点的图（POST /import 可引入）
    运行到该节点必然 RuntimeError('tool_executor is not set')——在保存 /
    导入 / 试跑期就以 error 阻断，而不是等到运行期节点失败。
    """
    result = PipelineCompiler.validate(graph, await _get_model_efforts())
    vision = await _get_model_vision()
    tool_wired = _executor_kwargs.get("tool_executor") is not None
    skill_wired = _executor_kwargs.get("skill_executor") is not None
    action_provider = _executor_kwargs.get("action_provider")
    try:
        actions = normalize_actions(action_provider()) if action_provider is not None else {}
    except Exception:
        log.exception("Pipeline action provider failed during validation")
        actions = {}
    for node in graph.nodes:
        action = actions.get(node.action_id) if node.type == NodeType.ACTION else None
        if (
            (node.type == NodeType.TASK or (action is not None and action.uses_model))
            and node.attachments
            and vision is not None
        ):
            selected = (node.model or graph.default_model or "auto").strip()
            if not selected or selected.lower() == "auto":
                if not any(vision.values()):
                    result.errors.append(
                        f"Task node '{node.id}' includes images but no vision-capable model is available"
                    )
            elif not vision.get(selected, False):
                result.errors.append(
                    f"Task node '{node.id}' includes images but model '{selected}' does not support vision"
                )
        if node.type == NodeType.TOOL and not tool_wired:
            result.errors.append(
                f"Tool node '{node.id}' is not runnable: tool_executor is "
                f"not wired in this deployment (use a task node with tools "
                f"instead)"
            )
        elif node.type == NodeType.SKILL and not skill_wired:
            result.errors.append(
                f"Skill node '{node.id}' is not runnable: skill_executor is "
                f"not wired in this deployment (use a task node with a skill "
                f"instead)"
            )
        elif node.type == NodeType.ACTION and node.action_id not in actions:
            result.errors.append(
                f"Action node '{node.id}' is not runnable: pipeline action "
                f"'{node.action_id}' is not installed"
            )
    if result.errors:
        result.valid = False
    return result


# ---------------------------------------------------------------------------
# 能力枚举
# ---------------------------------------------------------------------------

# 执行模式固定集合（与 GraphNode.mode / 前端 MODE_INFO 对齐）
_EXEC_MODES = ("react", "plan", "go")


@router.get("/capabilities")
async def get_capabilities() -> dict:
    """能力枚举：可用模型（含各自的 effort 档位）、工具、技能、执行模式。

    响应结构（契约）：
        {"models": [{"id": str, "efforts": [str, ...]}, ...],
         "tools": [str, ...], "skills": [str, ...], "modes": [str, ...]}

    models/tools/skills 由宿主注入的 capabilities_provider 提供（未注入
    或提供失败时为空列表），modes 恒为 react/plan/go。

    注意：本路由必须先于 GET /{graph_id} 注册（保持本函数在文件中的
    位置在其之前），否则 "capabilities" 会被当作 graph_id 匹配。
    """
    caps: dict[str, Any] = {
        "models": [],
        "tools": [],
        "skills": [],
        "actions": [],
        "modes": list(_EXEC_MODES),
    }
    if _capabilities_provider is not None:
        data: dict | None = None
        try:
            data = await _capabilities_provider()
        except Exception:
            log.exception("Capabilities provider failed")
        if isinstance(data, dict):
            for key in caps:
                value = data.get(key)
                if isinstance(value, list):
                    caps[key] = value
    action_provider = _executor_kwargs.get("action_provider")
    if action_provider is not None:
        try:
            caps["actions"] = [
                action.capability()
                for action in normalize_actions(action_provider()).values()
            ]
        except Exception:
            log.exception("Pipeline action provider failed")
    caps["actions"].sort(
        key=lambda item: (str(item.get("category", "")), str(item.get("title", "")))
    )
    return caps


# ---------------------------------------------------------------------------
# 草稿校验 / 内联试跑 / 导入
#
# 注意：以下顶层字面量路由（/validate、/run、/import）必须先于
# GET /{graph_id} 注册（保持函数在文件中的位置在其之前），
# 否则路径会被通配参数吞掉。
# ---------------------------------------------------------------------------


@router.post("/assets")
async def upload_asset(request: Request) -> dict[str, Any]:
    """Store one raw image body and return its content-addressed reference."""
    if _asset_store is None:
        raise HTTPException(status_code=500, detail="Asset store not initialized")
    try:
        ref = _asset_store.put(
            await request.body(),
            request.headers.get("content-type", ""),
            request.headers.get("x-file-name", ""),
        )
    except AssetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ref.model_dump()


@router.get("/assets/{digest}")
async def get_asset(digest: str) -> FileResponse:
    if _asset_store is None:
        raise HTTPException(status_code=500, detail="Asset store not initialized")
    try:
        path = _asset_store.by_digest(digest)
    except AssetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path)


@router.get("/run-artifacts/{run_id}/{name}")
async def get_run_artifact(run_id: str, name: str) -> FileResponse:
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")
    try:
        path = _store.run_artifact_path(run_id, name)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="artifact not found") from exc
    return FileResponse(path)


@router.post("/validate", response_model=ValidateResponse)
async def validate_draft(
    graph_data: dict[str, Any] = Body(...),
) -> ValidateResponse:
    """草稿免保存校验：body 直接传图 JSON，返回 errors/warnings。

    能力上下文（model -> 可用 effort 档位）由服务端从 capabilities
    数据构造后传给 compiler.validate，用于 effort-模型匹配校验。
    """
    graph = _parse_graph(graph_data)
    await _auto_resolve_schedules(graph)
    return await _validate_graph(graph)


@router.post("/run")
async def run_draft(request: InlineRunRequest) -> JSONResponse:
    """内联试跑：body 传图直接发起一次运行。

    管线本体不落盘；运行记录照常写（进度订阅、历史查询与正常 run
    一致，走 GET /{graph_id}/runs/{run_id}/events 等端点）。
    校验 errors 阻断试跑（400），warnings 随响应返回不阻断。
    """
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    graph = _parse_graph(request.graph)
    await _auto_resolve_schedules(graph)
    result = await _validate_graph(graph)
    if not result.valid:
        return _validation_failed(result)

    handle = _start_run(
        graph, request.trigger_input, budget_total=request.budget_total,
        entry_node_id=request.entry_node_id,
    )
    return JSONResponse({
        "run_id": handle.run_id,
        "graph_id": graph.graph_id,
        "status": "running",
        "warnings": result.warnings,
    })


@router.post("/import")
async def import_pipeline(graph_data: dict[str, Any] = Body(...)) -> dict:
    """导入：body 传完整图 JSON（导出文件原样），新建管线。

    始终分配新 graph_id 并重置版本，不覆盖既有管线；
    校验 errors 阻断导入（400），warnings 随响应返回。
    """
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    graph = _parse_graph(graph_data)
    await _auto_resolve_schedules(graph)
    result = await _validate_graph(graph)
    if not result.valid:
        return _validation_failed(result)

    created = _store.import_pipeline(graph)
    if _schedule_store is not None:
        _schedule_store.sync_graph(created)
    return {**_dump_graph(created), "warnings": result.warnings}


# ---------------------------------------------------------------------------
# Pipeline CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PipelineListItem])
async def list_pipelines() -> list[PipelineListItem]:
    """列出所有 pipeline。"""
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")
    return _store.list_pipelines()


@router.get("/{graph_id}", response_model=PipelineGraph)
async def get_pipeline(graph_id: str) -> PipelineGraph:
    """获取指定 pipeline。"""
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    graph = _store.get_pipeline(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{graph_id}' not found")
    return graph


@router.post("")
async def create_pipeline(request: CreatePipelineRequest) -> dict:
    """创建新 pipeline。

    保存前跑静态校验：errors 阻断（400 含 errors/warnings），
    warnings 不阻断、随响应顶层 warnings 键返回。
    """
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    graph = PipelineGraph(
        name=request.name,
        description=request.description,
        nodes=request.nodes,
        edges=request.edges,
        variables=request.variables,
        default_model=request.default_model or None,
        default_effort=request.default_effort or None,
    )

    await _auto_resolve_schedules(graph)
    result = await _validate_graph(graph)
    if not result.valid:
        return _validation_failed(result)

    created = _store.create_pipeline(graph)
    if _schedule_store is not None:
        _schedule_store.sync_graph(created)
    return {**_dump_graph(created), "warnings": result.warnings}


@router.put("/{graph_id}")
async def update_pipeline(graph_id: str, request: UpdatePipelineBody) -> dict:
    """更新 pipeline。

    - 乐观锁：body 带 version（客户端加载时的值）时，在 store 自动 +1
      之前与当前版本比对，不一致返回 409（含 current_version）；
      不带 version 跳过比对（旧客户端兼容）。
    - 保存前跑静态校验：errors 阻断（400），warnings 随响应返回。
    """
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    graph = _store.get_pipeline(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{graph_id}' not found")

    if request.version is not None and request.version != graph.version:
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"Version conflict: pipeline '{graph_id}' has been "
                    f"modified (current version {graph.version})"
                ),
                "current_version": graph.version,
            },
        )

    # 更新字段
    if request.name is not None:
        graph.name = request.name
    if request.description is not None:
        graph.description = request.description
    if request.nodes is not None:
        graph.nodes = request.nodes
    if request.edges is not None:
        graph.edges = request.edges
    if request.variables is not None:
        graph.variables = request.variables
    # None = 不更新；传 "" 清除图默认（归一化存为 None，is_auto 语义等价）
    if request.default_model is not None:
        graph.default_model = request.default_model or None
    if request.default_effort is not None:
        graph.default_effort = request.default_effort or None

    await _auto_resolve_schedules(graph)
    result = await _validate_graph(graph)
    if not result.valid:
        return _validation_failed(result)

    try:
        updated = _store.update_pipeline(graph, expected_version=request.version)
    except VersionConflictError as e:
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"Version conflict: pipeline '{graph_id}' has been "
                    f"modified (current version {e.current_version})"
                ),
                "current_version": e.current_version,
            },
        )
    if updated is None:
        raise HTTPException(status_code=500, detail="Failed to update pipeline")
    if _schedule_store is not None:
        _schedule_store.sync_graph(updated)
    return {**_dump_graph(updated), "warnings": result.warnings}


@router.delete("/{graph_id}")
async def delete_pipeline(graph_id: str) -> dict:
    """删除 pipeline。"""
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    success = _store.delete_pipeline(graph_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Pipeline '{graph_id}' not found")
    if _schedule_store is not None:
        _schedule_store.remove_graph(graph_id)
    return {"success": True, "graph_id": graph_id}


@router.get("/{graph_id}/export")
async def export_pipeline(graph_id: str) -> JSONResponse:
    """导出：返回完整图 JSON（Content-Disposition 触发浏览器下载）。

    响应体与 GET /{graph_id} 同构（含 graph_id/version/元信息），
    可直接喂给 POST /import。
    """
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    graph = _store.get_pipeline(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{graph_id}' not found")

    # ASCII 文件名兜底 + RFC 5987 编码的中文名
    filename_utf8 = quote(f"{graph.name or graph.graph_id}.json")
    return JSONResponse(
        content=_dump_graph(graph),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{graph.graph_id}.json"; '
                f"filename*=UTF-8''{filename_utf8}"
            ),
        },
    )


@router.post("/{graph_id}/duplicate")
async def duplicate_pipeline(graph_id: str) -> dict:
    """复制：服务端复制管线，名称加「(副本)」后缀（重名自动加序号）。"""
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    copy = _store.duplicate_pipeline(graph_id)
    if copy is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{graph_id}' not found")
    if _schedule_store is not None:
        _schedule_store.sync_graph(copy)
    return _dump_graph(copy)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


@router.post("/{graph_id}/validate", response_model=ValidateResponse)
async def validate_pipeline(graph_id: str) -> ValidateResponse:
    """校验已保存的 pipeline 图（草稿免保存校验请用 POST /validate）。"""
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    graph = _store.get_pipeline(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{graph_id}' not found")

    return await _validate_graph(graph)


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


import uuid as _uuid
from datetime import datetime as _dt, timedelta as _td, timezone as _tz


def _start_run(
    graph: PipelineGraph,
    trigger_input: dict[str, Any] | None,
    *,
    budget_total: int | None = None,
    resume_from: PipelineRunRecord | None = None,
    entry_node_id: str | None = None,
) -> _RunHandle:
    """启动一次后台执行并返回句柄。

    执行与任何 HTTP 连接解耦：请求立即返回 run_id，浏览器断线/刷新
    不影响执行；事件通过句柄缓冲，随时可重新订阅。
    entry_node_id 非空时从该节点向后执行子图（定时触发 / 从节点运行）。
    """
    run_id = f"run_{_uuid.uuid4().hex[:12]}"
    handle = _RunHandle(run_id, graph.graph_id)
    handle.entry_node_id = entry_node_id
    executor = _make_executor()

    def _on_executor_event(ev: dict) -> None:
        """事件双通道：内存缓冲/SSE 分发 + events.jsonl 持久化。

        事件流契约：runs/{run_id}.events.jsonl 每行一个 JSON 事件
        （executor 已附加 seq/ts）。thinking 增量体量大且纯装饰性，
        与内存缓冲同口径不落盘。持久化失败不影响执行。
        """
        handle.emit(ev)
        if _store is not None and ev.get("type") != "thinking":
            try:
                _store.append_run_event(run_id, ev)
            except Exception as e:
                log.debug("append_run_event failed for %s: %s", run_id, e)

    executor.on_event = _on_executor_event
    handle.executor = executor
    _active_runs[run_id] = handle

    async def _run_wrapper() -> None:
        rec: PipelineRunRecord | None = None
        try:
            rec = await executor.execute(
                graph,
                trigger_input,
                budget_total=budget_total,
                run_id=run_id,
                resume_from=resume_from,
                entry_node_id=entry_node_id,
            )
        except asyncio.CancelledError:
            # execute 内部已把 record 标为 INTERRUPTED 并 checkpoint
            rec = executor.current_run_record
        except Exception as e:  # execute 正常不抛；纯兜底
            rec = executor.current_run_record
            if rec is not None and rec.status == RunStatus.RUNNING:
                rec.status = RunStatus.FAILED
                rec.error_message = f"{type(e).__name__}: {e}"
                rec.completed_at = _dt.now(_tz.utc).isoformat()
            log.exception("Pipeline run crashed")
        finally:
            if rec is not None:
                handle.record = rec
                if _store is not None:
                    try:
                        _store.save_run(rec)
                        _store.delete_run_snapshot(rec.run_id)
                    except Exception:
                        log.exception("Failed to persist run record")
            else:
                handle.emit({
                    "type": "run_complete", "status": "interrupted",
                    "run_id": run_id,
                })
            handle.finish()
            _prune_handles()

    handle.task = asyncio.create_task(_run_wrapper())
    return handle


# ---------------------------------------------------------------------------
# 定时触发
# ---------------------------------------------------------------------------


def _has_active_run(graph_id: str, entry_node_id: str) -> bool:
    """是否存在同一 graph+入口的未结束 run（overlap 防护）。"""
    for h in _active_runs.values():
        if (
            h.graph_id == graph_id
            and h.entry_node_id == entry_node_id
            and not h.done.is_set()
        ):
            return True
    return False


def _adaptive_run_summary(record: PipelineRunRecord) -> dict[str, Any]:
    outputs = {
        key: str(value)[:500]
        for key, value in list(record.context_outputs.items())[-8:]
    }
    return {
        "status": record.status.value,
        "final_output": str(record.final_output)[:1000],
        "error": record.error_message,
        "node_statuses": {
            node_id: node_record.status.value
            for node_id, node_record in record.node_records.items()
        },
        "outputs": outputs,
    }


async def _light_agent_slot() -> tuple[str | None, str | None]:
    """按 Pipeline 能力上下文选择轻档模型与可用的 low effort。"""
    try:
        from mewcode.pipeline.auto_policy import converge_effort, pick_tier_model
        from mewcode.pipeline.models import MODEL_TIER_LIGHT

        models: list = []
        data: dict | None = None
        if _capabilities_provider is not None:
            data = await _capabilities_provider()
            got = data.get("models") if isinstance(data, dict) else None
            models = got if isinstance(got, list) else []
        session_model = (
            data.get("current_model") if isinstance(data, dict) else None
        )
        model, _reason = pick_tier_model(
            MODEL_TIER_LIGHT,
            _executor_kwargs.get("config"),
            models,
            session_model=session_model,
        )
        return model, converge_effort("low", model, models)
    except Exception:
        log.exception("[pipeline-schedule] failed to resolve review model")
        return None, None


def _schedule_review_criteria(
    graph: PipelineGraph,
    trigger: PipelineTrigger,
) -> str:
    if trigger.review_criteria.strip():
        return trigger.review_criteria.strip()

    targets: list[str] = []
    seen = {trigger.node_id}
    queue = [trigger.node_id]
    while queue:
        current = queue.pop(0)
        for edge in graph.get_outgoing_edges(current):
            if edge.to_node in seen:
                continue
            seen.add(edge.to_node)
            queue.append(edge.to_node)
            node = graph.get_node(edge.to_node)
            if node is None:
                continue
            if node.type == NodeType.TASK and node.goal.strip():
                name = node.title.strip() or node.id
                targets.append(f"{name}：{node.goal.strip()}")
            elif node.type == NodeType.GOAL and node.criteria.strip():
                targets.append(node.criteria.strip())

    base = "本轮运行状态为 completed，且所有实际执行节点均无失败。"
    if not targets:
        return base + "最终输出或可验证的外部结果应明确表明任务已经完成。"
    criteria = base + "还必须达成以下下游目标：\n- " + "\n- ".join(targets[:12])
    target_text = "\n".join(targets).casefold()
    if any(
        hint in target_text
        for hint in ("通知", "弹窗", "提醒", "toast", "notification", "notify", "声音", "播放")
    ):
        criteria += (
            "\n\n这是瞬时动作：以本轮执行时的系统调用成功凭据为准，不要求通知、"
            "窗口、声音或临时进程在审查时继续存在。"
        )
    return criteria


async def _review_scheduled_run(
    trigger: PipelineTrigger,
    graph: PipelineGraph,
    record: PipelineRunRecord,
) -> ScheduleReviewDecision:
    hard_result = structural_review(record)
    if hard_result is not None:
        return hard_result

    factory = _executor_kwargs.get("agent_factory")
    if factory is None:
        return ScheduleReviewDecision(
            True,
            "运行及节点状态检查通过；未配置 Agent，采用结构审查",
            "structure-fallback",
        )

    summary = _adaptive_run_summary(record)
    summary["run_id"] = record.run_id
    summary["node_results"] = {
        node_id: {
            "status": node.status.value,
            "result": node.result_summary[:1000],
            "error": node.error_message,
        }
        for node_id, node in record.node_records.items()
    }
    model, effort = await _light_agent_slot()
    criteria = _schedule_review_criteria(graph, trigger)
    try:
        result, _usage = await factory(
            prompt=build_schedule_review_prompt(
                criteria,
                summary,
            ),
            schema=None,
            model=model,
            effort=effort,
            isolation=None,
            on_event=None,
        )
        decision = parse_schedule_review_reply(str(result))
        if decision is not None:
            return reconcile_ephemeral_review(decision, criteria, summary)
        log.warning(
            "[pipeline-schedule] reviewer returned unparseable reply for %s",
            trigger.key,
        )
    except Exception:
        log.exception("[pipeline-schedule] reviewer failed for %s", trigger.key)

    # 审查服务本身异常时不重复执行已成功的副作用任务；结构审查仍是明确结论。
    return ScheduleReviewDecision(
        True,
        "运行及节点状态检查通过；Agent 审查不可用，采用结构审查",
        "structure-fallback",
    )


async def _renew_adaptive_trigger(
    trigger: PipelineTrigger,
    handle: _RunHandle,
    *,
    succeeded_override: bool | None = None,
) -> None:
    """Wait for an adaptive run and persist its next one-shot fire time."""
    await handle.done.wait()
    record = handle.record
    if record is None or _schedule_store is None:
        return
    current = _schedule_store.get(trigger.graph_id, trigger.node_id)
    if current is None or not current.adaptive or not current.enabled:
        return

    now = _dt.now(_tz.utc)
    succeeded = (
        succeeded_override
        if succeeded_override is not None
        else record.status == RunStatus.COMPLETED
    )
    decision = decide_by_rule(
        current.adaptive_policy, succeeded=succeeded, now=now
    )
    factory = _executor_kwargs.get("agent_factory")
    if decision is None and factory is not None:
        try:
            result, _usage = await factory(
                prompt=build_next_run_prompt(
                    current.adaptive_policy,
                    _adaptive_run_summary(record),
                    now=now,
                ),
                schema=None,
                model=None,
                effort="low",
                isolation=None,
                on_event=None,
            )
            decision = parse_next_run_reply(str(result), now=now)
        except Exception:
            log.exception(
                "[pipeline-schedule] adaptive decision failed for %s",
                current.key,
            )

    if decision is None:
        fallback = compute_next_fire(current.cron, now) if current.cron else None
        if fallback is None and current.interval_seconds:
            fallback = now + _td(seconds=current.interval_seconds)
        if fallback is None:
            fallback = now + (_td(hours=1) if not succeeded else _td(days=1))
        reason = "自适应决策不可用，按初始排期保底续约"
        next_fire_at = fallback
    else:
        reason = decision.reason
        next_fire_at = decision.next_fire_at

    if _schedule_store.schedule_next(
        current.graph_id, current.node_id, next_fire_at, reason
    ):
        log.info(
            "[pipeline-schedule] %s renewed for %s (%s)",
            current.key, next_fire_at.isoformat(), reason,
        )


async def _finalize_scheduled_trigger(
    trigger: PipelineTrigger,
    graph: PipelineGraph,
    handle: _RunHandle,
) -> None:
    """等待真实 run 结束；可选 Agent 审查后更新运行次数与下一轮。"""
    await handle.done.wait()
    if _schedule_store is None:
        return
    current = _schedule_store.get(trigger.graph_id, trigger.node_id)
    if (
        current is None
        or current.in_flight_run_id != handle.run_id
    ):
        return
    if current.review_enabled:
        if not _schedule_store.mark_reviewing(
            current.graph_id, current.node_id, handle.run_id
        ):
            return

    record = handle.record
    if record is None:
        decision = ScheduleReviewDecision(
            False,
            "运行结束但未生成可审查记录",
            "structure",
        )
    elif current.review_enabled:
        decision = await _review_scheduled_run(current, graph, record)
    else:
        decision = structural_review(record) or ScheduleReviewDecision(
            True,
            "运行及全部执行节点均已真实结束",
            "structure",
        )

    if not _schedule_store.finish_review(
        current.graph_id,
        current.node_id,
        handle.run_id,
        passed=decision.passed,
        reason=f"{decision.reason}（{decision.source}）",
    ):
        return
    log.info(
        "[pipeline-schedule] %s review %s for run %s: %s",
        current.key,
        "passed" if decision.passed else "failed",
        handle.run_id,
        decision.reason,
    )

    refreshed = _schedule_store.get(current.graph_id, current.node_id)
    if refreshed is not None and refreshed.adaptive:
        await _renew_adaptive_trigger(
            refreshed,
            handle,
            succeeded_override=decision.passed,
        )


def _fire_trigger(trigger: PipelineTrigger) -> bool:
    """定时触发回调：从 schedule 节点向后发起一次子图 run。

    - 图/节点已删除 → 同步注册后静默返回（自愈，不报错）；
    - 所有定时任务都强制串行，等待真实 run 结束后才增加运行次数；
    - 开启审查时在结构完成检查后再由 Agent 验收结果；
    - 未生成 run_id 的启动失败由调度器退避重试，不消耗当前次数；
    - 触发元信息放进 trigger_input.trigger，下游可用
      {{input.trigger.fired_at}} / {{input.trigger.run_index}} 引用。
    """
    if _store is None:
        return True
    graph = _store.get_pipeline(trigger.graph_id)
    if graph is None:
        log.info(
            "[pipeline-schedule] graph %s gone; removing triggers",
            trigger.graph_id,
        )
        if _schedule_store is not None:
            _schedule_store.remove_graph(trigger.graph_id)
        return True
    node = graph.get_node(trigger.node_id)
    if node is None or node.type != NodeType.SCHEDULE:
        log.info(
            "[pipeline-schedule] node %s gone; resyncing graph %s",
            trigger.node_id, trigger.graph_id,
        )
        if _schedule_store is not None:
            _schedule_store.sync_graph(graph)
        return True

    if _schedule_store is None:
        log.error("[pipeline-schedule] schedule store is unavailable")
        return False
    active = _has_active_run(trigger.graph_id, trigger.node_id)
    if trigger.in_flight_run_id or active:
        log.info(
            "[pipeline-schedule] %s waiting for run %s",
            trigger.key,
            trigger.in_flight_run_id,
        )
        return False

    handle = _start_run(
        graph,
        {
            "trigger": {
                "type": "schedule",
                "node_id": trigger.node_id,
                "fired_at": _dt.now(_tz.utc).isoformat(),
                "schedule_text": trigger.schedule_text,
                "cron": trigger.cron,
                "run_at": trigger.run_at,
                "interval_seconds": trigger.interval_seconds,
                "max_runs": trigger.max_runs,
                "adaptive": trigger.adaptive,
                "adaptive_policy": trigger.adaptive_policy,
                "review_enabled": trigger.review_enabled,
                "run_index": trigger.run_count + 1,
                "attempt_index": trigger.attempt_count + 1,
            }
        },
        entry_node_id=trigger.node_id,
    )
    log.info(
        "[pipeline-schedule] %s fired -> run %s", trigger.key, handle.run_id
    )
    if not _schedule_store.mark_in_flight(
        trigger.graph_id, trigger.node_id, handle.run_id
    ):
        if handle.task is not None:
            handle.task.cancel()
        log.error(
            "[pipeline-schedule] failed to reserve run %s",
            handle.run_id,
        )
        return False
    task = asyncio.create_task(
        _finalize_scheduled_trigger(trigger, graph, handle)
    )
    _adaptive_tasks.add(task)
    task.add_done_callback(_adaptive_tasks.discard)
    return True


@router.get("/{graph_id}/schedules")
async def list_schedules(graph_id: str) -> list[dict]:
    """列出该图已注册的定时触发（含下次触发时间与触发统计）。"""
    if _store is None or _schedule_store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")
    if _store.get_pipeline(graph_id) is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{graph_id}' not found")

    result: list[dict] = []
    for t in _schedule_store.list(graph_id):
        nf = t.next_fire()
        if t.adaptive:
            summary = "自适应：" + t.adaptive_policy
        elif t.interval_seconds:
            start_at = _dt.fromisoformat(t.run_at.replace("Z", "+00:00"))
            limit = f"，共 {t.max_runs} 次" if t.max_runs else ""
            progress = (
                f"（已运行 {t.run_count}/{t.max_runs} 次）"
                if t.max_runs
                else f"（已运行 {t.run_count} 次）"
            )
            summary = (
                f"{start_at.astimezone():%Y-%m-%d %H:%M:%S} 起"
                f"每 {t.interval_seconds} 秒{limit}{progress}"
            )
        elif t.run_at:
            run_at = _dt.fromisoformat(t.run_at.replace("Z", "+00:00"))
            summary = (
                f"{run_at.astimezone():%Y-%m-%d %H:%M} "
                f"{'已运行 1 次' if t.run_count else '等待首轮运行'}"
            )
        else:
            summary = f"{describe_cron(t.cron)}（已运行 {t.run_count} 次）"
        result.append({
            "node_id": t.node_id,
            "cron": t.cron,
            "run_at": t.run_at,
            "one_shot_completed": t.one_shot_completed,
            "interval_seconds": t.interval_seconds,
            "max_runs": t.max_runs,
            "schedule_text": t.schedule_text,
            "summary": summary,
            "enabled": t.enabled,
            "overlap_policy": t.overlap_policy,
            "review_enabled": t.review_enabled,
            "review_criteria": t.review_criteria,
            "adaptive": t.adaptive,
            "adaptive_policy": t.adaptive_policy,
            "next_fire_reason": t.next_fire_reason,
            "last_fired_at": t.last_fired_at,
            "run_count": t.run_count,
            "attempt_count": t.attempt_count,
            "launch_retry_count": t.launch_retry_count,
            "last_launch_error": t.last_launch_error,
            "next_launch_at": t.next_launch_at or None,
            "in_flight_run_id": t.in_flight_run_id or None,
            "in_flight_phase": t.in_flight_phase or None,
            "last_review_status": t.last_review_status or None,
            "last_review_reason": t.last_review_reason,
            "last_reviewed_at": t.last_reviewed_at or None,
            "next_fire": nf.astimezone().isoformat() if nf else None,
        })
    return result


class ToggleScheduleBody(BaseModel):
    """启停定时触发的请求体。"""

    enabled: bool


class SchedulePreviewBody(BaseModel):
    """排期预览请求体。"""

    schedule_text: str


async def _resolve_schedule_async(text: str) -> ResolvedSchedule | None:
    """两级解析：规则层命中直接返回；否则轻档 LLM 兜底。

    LLM 选模与 executor 辅助调用同源（pick_tier_model 轻档 + low）；
    agent_factory 未接线 / 调用失败 / 回复无法解析时返回 None。
    """
    resolved = resolve_schedule_text(text)
    if resolved is not None:
        return resolved

    factory = _executor_kwargs.get("agent_factory")
    if factory is None:
        return None

    model, effort = await _light_agent_slot()

    now_str = _dt.now().astimezone().strftime("%Y-%m-%d %H:%M %A %z")
    try:
        result, _usage = await factory(
            prompt=build_llm_prompt(text, now_str),
            schema=None,
            model=model,
            effort=effort,
            isolation=None,
            on_event=None,
        )
    except Exception:
        log.exception("[pipeline-schedule] LLM resolve failed")
        return None
    resolved = parse_llm_reply(str(result))
    if resolved is not None:
        resolved.model = model or "default"
    return resolved


@router.post("/schedules/preview")
async def preview_schedule(body: SchedulePreviewBody) -> dict:
    """排期预览：自然语言/cron → 解析结果 + 未来 3 次触发时间。

    前端在 schedule 节点编辑时调用：确认后把 cron 回写 resolved_cron
    随图保存。解析不了时 resolved=false + reason，不报 5xx。
    """
    text = body.schedule_text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="schedule_text is empty")

    resolved = await _resolve_schedule_async(text)
    if resolved is None:
        return {
            "resolved": False,
            "schedule_text": text,
            "reason": (
                "规则和排期 Agent 都没能确定可执行时间。请补充起点、间隔、"
                "次数或日期，例如「从12:03开始每10秒，共5次」"
            ),
        }
    timing_note = ""
    if resolved.interval_seconds:
        run_at = _dt.fromisoformat(resolved.run_at.replace("Z", "+00:00"))
        now = _dt.now(_tz.utc)
        # 间隔任务按实际完成推进，不预占一串僵硬的墙钟槽位。起点已过时
        # 代表首轮立即具备触发资格，后续每轮从上一轮审查结束重新计时。
        fires = [max(run_at, now)]
        timing_note = (
            f"首轮不早于起点；后续每轮在上一轮实际运行并审查结束后，"
            f"再等待 {resolved.interval_seconds} 秒"
        )
    elif resolved.run_at:
        run_at = _dt.fromisoformat(resolved.run_at.replace("Z", "+00:00"))
        if run_at < _dt.now(_tz.utc) - _td(minutes=1):
            return {
                "resolved": False,
                "schedule_text": text,
                "reason": f"一次性执行时间 {run_at.astimezone():%Y-%m-%d %H:%M} 已经过期",
            }
        fires = [run_at]
    else:
        fires = compute_next_fires(resolved.cron, 3)
    return {
        "resolved": True,
        "schedule_text": text,
        "cron": resolved.cron,
        "run_at": resolved.run_at,
        "interval_seconds": resolved.interval_seconds,
        "max_runs": resolved.max_runs,
        "schedule_type": (
            "interval" if resolved.interval_seconds
            else "once" if resolved.run_at
            else "cron"
        ),
        "recommended_overlap_policy": "skip",
        "summary": resolved.summary,
        "source": resolved.source,
        "model": resolved.model or None,
        "timing_mode": (
            "after_completion" if resolved.interval_seconds else "wall_clock"
        ),
        "timing_note": timing_note,
        "next_fires": [f.astimezone().isoformat() for f in fires],
    }


@router.post("/{graph_id}/schedules/{node_id}/toggle")
async def toggle_schedule(
    graph_id: str, node_id: str, body: ToggleScheduleBody
) -> dict:
    """启停一个定时触发。

    图是事实来源：同时更新节点的 schedule.enabled 并重新同步注册，
    避免下次保存图时把开关状态冲回去（会 bump 图 version）。
    """
    if _store is None or _schedule_store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    graph = _store.get_pipeline(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{graph_id}' not found")
    node = graph.get_node(node_id)
    if node is None or node.type != NodeType.SCHEDULE or node.schedule is None:
        raise HTTPException(
            status_code=404,
            detail=f"Schedule node '{node_id}' not found in graph",
        )

    node.schedule.enabled = body.enabled
    updated = _store.update_pipeline(graph)
    if updated is None:
        raise HTTPException(status_code=500, detail="Failed to update pipeline")
    _schedule_store.sync_graph(updated)
    return {
        "graph_id": graph_id,
        "node_id": node_id,
        "enabled": body.enabled,
        "version": updated.version,
    }


def _sse(ev: dict) -> str:
    seq = ev.get("_seq", 0)
    payload = {k: v for k, v in ev.items() if k != "_seq"}
    return f"id: {seq}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _subscribe(handle: _RunHandle, since: int):
    """订阅一个 run 的事件流：先补发缓冲，再实时跟随。

    断开只影响本订阅者；run 在后台继续执行。
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    handle.subscribers.add(q)
    sent = since
    try:
        for ev in list(handle.events):
            if ev["_seq"] > sent:
                yield _sse(ev)
                sent = ev["_seq"]
        while True:
            if handle.done.is_set() and q.empty():
                break
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": ping\n\n"  # 保活，防代理断连
                continue
            if ev.get("_seq", -1) <= sent:
                continue  # 补发阶段已发送过 / 完成哨兵
            yield _sse(ev)
            sent = ev["_seq"]
    finally:
        handle.subscribers.discard(q)


def _replay_events(events: list[dict], since: int) -> StreamingResponse:
    """已结束 run 的完整回放：直接重放 events.jsonl 的真实事件流。

    与合成回放（_replay_finished）不同，node_start 的 resolved_* 字段、
    edge_taken、warning 等全部原样保留；executor 附加的 seq 作为 SSE id，
    支持 Last-Event-ID / since 续传。
    """

    async def _gen():
        for ev in events:
            seq = ev.get("seq")
            seq = seq if isinstance(seq, int) else 0
            if seq <= since:
                continue
            yield _sse({**ev, "replay": True, "_seq": seq})

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _replay_finished(record: PipelineRunRecord):
    """已结束（不在内存中）的 run：从持久化记录合成事件回放。

    仅在 events.jsonl 缺失（老 run / 持久化失败）时作为兜底，
    正常路径走 _replay_events。"""
    async def _gen():
        seq = 0

        def ev(payload: dict) -> str:
            nonlocal seq
            seq += 1
            payload["_seq"] = seq
            return _sse(payload)

        yield ev({
            "type": "run_start", "graph_id": record.graph_id,
            "name": record.graph_name, "run_id": record.run_id,
            "source": "schedule" if (record.trigger_input.get("trigger") or {}).get("type") == "schedule" else "manual",
            "replay": True,
        })
        for nid, nr in record.node_records.items():
            yield ev({
                "type": "node_start", "node_id": nid,
                "node_type": nr.node_type.value, "title": nid, "replay": True,
            })
            if nr.result_summary:
                yield ev({
                    "type": "node_output", "node_id": nid,
                    "output": nr.result_summary, "replay": True,
                })
            yield ev({
                "type": "node_done", "node_id": nid,
                "status": nr.status.value, "error": nr.error_message,
                "replay": True,
            })
        yield ev({
            "type": "run_complete", "status": record.status.value,
            "error": record.error_message, "run_id": record.run_id,
            "replay": True,
        })

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{graph_id}/run")
async def run_pipeline(graph_id: str, request: RunPipelineRequest) -> JSONResponse:
    """启动 pipeline 后台执行，立即返回 run_id。

    进度通过 GET /{graph_id}/runs/{run_id}/events 订阅（可断线重连）。
    """
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    graph = _store.get_pipeline(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{graph_id}' not found")

    if (
        request.entry_node_id is not None
        and graph.get_node(request.entry_node_id) is None
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Entry node '{request.entry_node_id}' not found in graph",
        )

    handle = _start_run(
        graph, request.trigger_input, budget_total=request.budget_total,
        entry_node_id=request.entry_node_id,
    )
    return JSONResponse({
        "run_id": handle.run_id,
        "graph_id": graph_id,
        "status": "running",
    })


@router.get("/{graph_id}/runs/{run_id}/events")
async def run_events(
    graph_id: str, run_id: str, request: Request, since: int = 0
) -> StreamingResponse:
    """订阅 run 的事件流（SSE）。

    - 活跃 run：补发缓冲事件后实时跟随；断线重连时浏览器自动带
      Last-Event-ID，从断点继续，不重启执行。
    - 已结束 run：优先重放 events.jsonl 的真实事件流（含 resolved_* /
      edge_taken）；文件缺失时从持久化记录合成回放兜底。
    """
    # Last-Event-ID 优先（EventSource 自动重连时携带）
    last_id = request.headers.get("last-event-id")
    if last_id:
        try:
            since = max(since, int(last_id))
        except ValueError:
            pass

    handle = _active_runs.get(run_id)
    if handle is not None:
        return StreamingResponse(
            _subscribe(handle, since),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")
    record = _store.find_run(graph_id, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    stored_events = _store.load_run_events(run_id)
    if stored_events:
        return _replay_events(stored_events, since)
    return _replay_finished(record)


@router.post("/{graph_id}/runs/{run_id}/cancel")
async def cancel_run(graph_id: str, run_id: str) -> dict:
    """取消运行中的 run（标记为 interrupted，可此后断点续跑）。"""
    handle = _active_runs.get(run_id)
    if handle is None or handle.task is None:
        raise HTTPException(status_code=404, detail=f"Active run '{run_id}' not found")
    if handle.done.is_set():
        return {"run_id": run_id, "status": "already_finished"}
    handle.task.cancel()
    return {"run_id": run_id, "status": "cancelling"}


@router.post("/{graph_id}/runs/{run_id}/resume")
async def resume_run(graph_id: str, run_id: str) -> JSONResponse:
    """从失败/中断的 run 断点续跑。

    已完成节点的结果直接复用，失败与未执行的节点重新执行；
    产生一个新的 run_id（resumed_from 指向来源）。
    """
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    active = _active_runs.get(run_id)
    if active is not None and not active.done.is_set():
        raise HTTPException(status_code=409, detail=f"Run '{run_id}' is still running")

    source: PipelineRunRecord | None = None
    if active is not None:
        source = active.current_record()
    if source is None:
        source = _store.find_run(graph_id, run_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    if source.status == RunStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Run already completed")

    graph = _store.get_pipeline(graph_id)
    if graph is None:
        # 内联试跑（POST /run）的管线本体不落盘，run 结束后无图可续；
        # 已删除管线的历史 run 同理
        raise HTTPException(
            status_code=404,
            detail=(
                f"Pipeline '{graph_id}' not found; inline draft runs "
                f"(and runs of deleted pipelines) cannot be resumed"
            ),
        )

    handle = _start_run(
        graph,
        source.trigger_input,
        resume_from=source,
        entry_node_id=source.entry_node_id,
    )
    completed = sum(
        1 for r in source.node_records.values() if r.status == NodeStatus.COMPLETED
    )
    return JSONResponse({
        "run_id": handle.run_id,
        "graph_id": graph_id,
        "status": "running",
        "resumed_from": run_id,
        "reused_nodes": completed,
    })


@router.get("/{graph_id}/stream")
async def stream_run(graph_id: str):
    """（兼容端点）启动一次执行并以 SSE 跟随。

    与旧版的关键差异：客户端断开只是取消订阅，run 在后台继续执行，
    不再被 task.cancel() 杀掉。新前端请改用 POST /run + /runs/{id}/events。
    """
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline not initialized")

    graph = _store.get_pipeline(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{graph_id}' not found")

    handle = _start_run(graph, {})
    return StreamingResponse(
        _subscribe(handle, 0),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{graph_id}/runs", response_model=list[PipelineRunRecord])
async def list_runs(graph_id: str, limit: int = 50) -> list[PipelineRunRecord]:
    """列出 pipeline 的执行历史（含正在运行的）。"""
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    stored = _store.list_runs(graph_id, limit=limit)
    by_id = {r.run_id: r for r in stored}
    # 活跃 run 的实时状态覆盖/补充
    for handle in _active_runs.values():
        if handle.graph_id != graph_id:
            continue
        rec = handle.current_record()
        if rec is not None:
            by_id[rec.run_id] = rec
    records = sorted(by_id.values(), key=lambda r: r.started_at or "", reverse=True)
    return records[:limit]


@router.get("/{graph_id}/runs/{run_id}", response_model=PipelineRunRecord)
async def get_run(graph_id: str, run_id: str) -> PipelineRunRecord:
    """获取单次执行记录（活跃 run 返回实时状态）。"""
    if _store is None:
        raise HTTPException(status_code=500, detail="Pipeline store not initialized")

    handle = _active_runs.get(run_id)
    if handle is not None:
        rec = handle.current_record()
        if rec is not None:
            return rec

    run_record = _store.find_run(graph_id, run_id)
    if run_record is None:
        raise HTTPException(
            status_code=404, detail=f"Run '{run_id}' not found for pipeline '{graph_id}'"
        )
    return run_record
