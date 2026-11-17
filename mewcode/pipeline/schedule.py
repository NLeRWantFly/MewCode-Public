"""Pipeline 定时触发：SCHEDULE 节点的注册存储与后台调度循环。

- PipelineTrigger: 一条已注册的触发（graph 内的一个 schedule 节点）
- PipelineScheduleStore: 触发注册的持久化（.mewcode/pipelines/schedules.json），
  以图为单位与 SCHEDULE 节点做 diff 同步，图是唯一事实来源，
  store 只额外保留触发统计（last_fired_at / run_count）
- PipelineScheduler: 后台 asyncio 轮询循环，到期触发回调
  （回调由 api 层注入：从该节点向后发起一次子图 run）

cron 解析/下次触发计算复用 mewcode.scheduler.cron。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from mewcode.pipeline.models import NodeType, PipelineGraph
from mewcode.scheduler.cron import CronExpression

log = logging.getLogger(__name__)

SCHEDULES_FILE = ".mewcode/pipelines/schedules.json"

# 秒级间隔调度需要亚 10 秒响应；空闲轮询只做一次内存扫描。
_CHECK_INTERVAL = 1
_MAX_LAUNCH_RETRY_DELAY = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(iso_str: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def compute_next_fire(cron: str, after: datetime | None = None) -> datetime | None:
    """按本地时区语义计算 cron 的下次触发（返回 UTC aware）。

    「每天10点」指本地墙钟10点：CronExpression.next_fire 只做墙钟算术，
    这里把基准时间换成本地墙钟喂入求值，结果再换回 UTC，
    与 get_due 的 UTC 比较口径一致。cron 非法返回 None。
    """
    try:
        expr = CronExpression.parse(cron)
    except Exception:
        return None
    base = after or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    local = base.astimezone()  # 本地时区（当时偏移）
    # 本地墙钟以 naive-as-UTC 喂入（next_fire 内部只比对字段值）
    wall = local.replace(tzinfo=timezone.utc)
    nf = expr.next_fire(wall)
    if nf is None:
        return None
    # 结果是本地墙钟值：重标回本地时区后换算 UTC
    return nf.replace(tzinfo=local.tzinfo).astimezone(timezone.utc)


def compute_next_fires(
    cron: str, count: int = 3, after: datetime | None = None
) -> list[datetime]:
    """连续计算未来 count 次触发时间（UTC aware，preview 回显用）。"""
    fires: list[datetime] = []
    cursor = after
    for _ in range(count):
        nf = compute_next_fire(cron, cursor)
        if nf is None:
            break
        fires.append(nf)
        cursor = nf
    return fires


def compute_interval_fires(
    start_at: datetime,
    interval_seconds: int,
    *,
    count: int = 5,
    max_runs: int = 0,
) -> list[datetime]:
    """计算间隔调度的前几次理论触发点，供预览与测试使用。"""
    if interval_seconds <= 0 or count <= 0:
        return []
    total = min(count, max_runs) if max_runs else count
    return [
        start_at + timedelta(seconds=interval_seconds * index)
        for index in range(total)
    ]


@dataclass
class PipelineTrigger:
    """一条已注册的定时触发（对应 graph 内的一个 schedule 节点）。"""

    graph_id: str
    node_id: str
    cron: str
    run_at: str = ""
    one_shot_completed: bool = False
    interval_seconds: int = 0
    max_runs: int = 0
    schedule_text: str = ""
    enabled: bool = True
    overlap_policy: str = "auto"
    review_enabled: bool = True
    review_criteria: str = ""
    adaptive: bool = False
    adaptive_policy: str = ""
    next_fire_at: str = ""
    next_fire_reason: str = ""
    last_fired_at: str = ""
    run_count: int = 0
    """已真实运行并审查通过的次数。"""
    attempt_count: int = 0
    """已经成功创建 run_id 的实际启动次数。"""
    launch_retry_count: int = 0
    last_launch_error: str = ""
    next_launch_at: str = ""
    in_flight_run_id: str = ""
    in_flight_phase: str = ""
    last_review_status: str = ""
    last_review_reason: str = ""
    last_reviewed_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.graph_id}:{self.node_id}"

    def next_fire(self, after: datetime | None = None) -> datetime | None:
        """计算下一次触发时间（本地时区语义；cron 非法时返回 None）。"""
        if self.in_flight_run_id:
            return None
        launch_retry = _parse_iso(self.next_launch_at)
        if launch_retry is not None:
            return launch_retry
        if self.adaptive:
            return _parse_iso(self.next_fire_at) if self.next_fire_at else None
        if self.interval_seconds:
            if self.max_runs and self.run_count >= self.max_runs:
                return None
            start = _parse_iso(self.run_at)
            if start is None:
                return None
            last = _parse_iso(self.last_fired_at)
            if last is not None:
                return last + timedelta(seconds=self.interval_seconds)
            if self.run_count == 0:
                return start
            return start + timedelta(seconds=self.interval_seconds * self.run_count)
        if self.run_at:
            return None if self.one_shot_completed else _parse_iso(self.run_at)
        return compute_next_fire(self.cron, after)

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "node_id": self.node_id,
            "cron": self.cron,
            "run_at": self.run_at,
            "one_shot_completed": self.one_shot_completed,
            "interval_seconds": self.interval_seconds,
            "max_runs": self.max_runs,
            "schedule_text": self.schedule_text,
            "enabled": self.enabled,
            "overlap_policy": self.overlap_policy,
            "review_enabled": self.review_enabled,
            "review_criteria": self.review_criteria,
            "adaptive": self.adaptive,
            "adaptive_policy": self.adaptive_policy,
            "next_fire_at": self.next_fire_at,
            "next_fire_reason": self.next_fire_reason,
            "last_fired_at": self.last_fired_at,
            "run_count": self.run_count,
            "attempt_count": self.attempt_count,
            "launch_retry_count": self.launch_retry_count,
            "last_launch_error": self.last_launch_error,
            "next_launch_at": self.next_launch_at,
            "in_flight_run_id": self.in_flight_run_id,
            "in_flight_phase": self.in_flight_phase,
            "last_review_status": self.last_review_status,
            "last_review_reason": self.last_review_reason,
            "last_reviewed_at": self.last_reviewed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PipelineTrigger:
        return cls(
            graph_id=data.get("graph_id", ""),
            node_id=data.get("node_id", ""),
            cron=data.get("cron", ""),
            run_at=data.get("run_at", ""),
            one_shot_completed=data.get("one_shot_completed", False),
            interval_seconds=data.get("interval_seconds", 0),
            max_runs=data.get("max_runs", 0),
            schedule_text=data.get("schedule_text", ""),
            enabled=data.get("enabled", True),
            overlap_policy=data.get("overlap_policy", "skip"),
            # 旧版持久化的 run_count 是派发数，不能冒充已审查完成数。
            review_enabled=data.get("review_enabled", False),
            review_criteria=data.get("review_criteria", ""),
            adaptive=data.get("adaptive", False),
            adaptive_policy=data.get("adaptive_policy", ""),
            next_fire_at=data.get("next_fire_at", ""),
            next_fire_reason=data.get("next_fire_reason", ""),
            last_fired_at=data.get("last_fired_at", ""),
            run_count=data.get("run_count", 0),
            attempt_count=data.get("attempt_count", 0),
            launch_retry_count=data.get("launch_retry_count", 0),
            last_launch_error=data.get("last_launch_error", ""),
            next_launch_at=data.get("next_launch_at", ""),
            in_flight_run_id=data.get("in_flight_run_id", ""),
            in_flight_phase=data.get("in_flight_phase", ""),
            last_review_status=data.get("last_review_status", ""),
            last_review_reason=data.get("last_review_reason", ""),
            last_reviewed_at=data.get("last_reviewed_at", ""),
        )


class PipelineScheduleStore:
    """触发注册的持久化存储（图保存时 diff 同步）。"""

    def __init__(self, work_dir: str) -> None:
        self._file_path = Path(work_dir) / SCHEDULES_FILE
        self._triggers: dict[str, PipelineTrigger] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # 同步（图是事实来源）
    # ------------------------------------------------------------------

    def sync_graph(self, graph: PipelineGraph) -> list[PipelineTrigger]:
        """按图中 SCHEDULE 节点重建该图的触发注册。

        - 只注册 resolved_cron、resolved_at 或自适应策略非空的节点；
        - 节点仍在且 key 相同 → 保留触发统计（last_fired_at/run_count）；
        - 图里已删除的节点 → 注册一并移除。

        Returns:
            该图当前注册的触发列表。
        """
        self._ensure_loaded()
        old = {
            k: t for k, t in self._triggers.items()
            if t.graph_id == graph.graph_id
        }
        for k in old:
            del self._triggers[k]

        current: list[PipelineTrigger] = []
        for node in graph.nodes:
            if node.type != NodeType.SCHEDULE or node.schedule is None:
                continue
            cfg = node.schedule
            if (
                not cfg.resolved_cron
                and not cfg.resolved_at
                and not (cfg.adaptive and cfg.adaptive_policy)
            ):
                continue
            trigger = PipelineTrigger(
                graph_id=graph.graph_id,
                node_id=node.id,
                cron=cfg.resolved_cron,
                run_at=cfg.resolved_at,
                interval_seconds=cfg.resolved_interval_seconds,
                max_runs=cfg.resolved_max_runs,
                schedule_text=cfg.schedule_text,
                enabled=cfg.enabled,
                overlap_policy=cfg.overlap_policy,
                review_enabled=cfg.review_enabled,
                review_criteria=cfg.review_criteria,
                adaptive=cfg.adaptive,
                adaptive_policy=cfg.adaptive_policy,
            )
            prev = old.get(trigger.key)
            if prev is not None:
                same_interval = (
                    prev.run_at == trigger.run_at
                    and prev.interval_seconds == trigger.interval_seconds
                    and prev.max_runs == trigger.max_runs
                )
                same_count_semantics = (
                    prev.review_enabled == trigger.review_enabled
                )
                if (
                    (not trigger.interval_seconds or same_interval)
                    and same_count_semantics
                ):
                    trigger.last_fired_at = prev.last_fired_at
                    trigger.run_count = prev.run_count
                    trigger.attempt_count = prev.attempt_count
                    trigger.launch_retry_count = prev.launch_retry_count
                    trigger.last_launch_error = prev.last_launch_error
                    trigger.next_launch_at = prev.next_launch_at
                    trigger.in_flight_run_id = prev.in_flight_run_id
                    trigger.in_flight_phase = prev.in_flight_phase
                    trigger.last_review_status = prev.last_review_status
                    trigger.last_review_reason = prev.last_review_reason
                    trigger.last_reviewed_at = prev.last_reviewed_at
                if prev.run_at == trigger.run_at and same_count_semantics:
                    trigger.one_shot_completed = prev.one_shot_completed
                if prev.adaptive and trigger.adaptive:
                    trigger.next_fire_at = prev.next_fire_at
                    trigger.next_fire_reason = prev.next_fire_reason
            if (
                trigger.adaptive
                and not trigger.next_fire_at
                and (prev is None or not prev.adaptive)
            ):
                first_fire = (
                    _parse_iso(trigger.run_at)
                    if trigger.run_at else compute_next_fire(trigger.cron)
                )
                if first_fire is not None:
                    trigger.next_fire_at = first_fire.isoformat()
                    trigger.next_fire_reason = "按初始排期首次触发"
            self._triggers[trigger.key] = trigger
            current.append(trigger)

        self._save()
        return current

    def recover_in_flight(self) -> int:
        """服务重启后释放失去运行句柄的轮次，且不把它算作完成。"""
        self._ensure_loaded()
        recovered = 0
        now = _now_iso()
        for trigger in self._triggers.values():
            if not trigger.in_flight_run_id:
                continue
            trigger.in_flight_run_id = ""
            trigger.in_flight_phase = ""
            trigger.last_fired_at = now
            trigger.last_review_status = "interrupted"
            trigger.last_review_reason = "服务重启中断了等待；本轮未计数，将重新执行"
            trigger.last_reviewed_at = now
            recovered += 1
        if recovered:
            self._save()
        return recovered

    def recover_pending_adaptive(self) -> int:
        """Re-arm adaptive triggers left between fire and renewal after a restart."""
        self._ensure_loaded()
        recovered = 0
        now = datetime.now(timezone.utc)
        for trigger in self._triggers.values():
            if not trigger.enabled or not trigger.adaptive or trigger.next_fire_at:
                continue
            next_fire = compute_next_fire(trigger.cron, now) if trigger.cron else None
            if next_fire is None:
                next_fire = now + timedelta(hours=1)
            trigger.next_fire_at = next_fire.isoformat()
            trigger.next_fire_reason = "服务重启后按初始排期恢复自适应续约"
            recovered += 1
        if recovered:
            self._save()
        return recovered

    def remove_graph(self, graph_id: str) -> None:
        """移除某图的全部触发注册（图删除时调用）。"""
        self._ensure_loaded()
        keys = [k for k, t in self._triggers.items() if t.graph_id == graph_id]
        for k in keys:
            del self._triggers[k]
        if keys:
            self._save()

    # ------------------------------------------------------------------
    # 查询与触发
    # ------------------------------------------------------------------

    def list(self, graph_id: str | None = None) -> list[PipelineTrigger]:
        """列出触发注册（可按图过滤）。"""
        self._ensure_loaded()
        return [
            t for t in self._triggers.values()
            if graph_id is None or t.graph_id == graph_id
        ]

    def get(self, graph_id: str, node_id: str) -> PipelineTrigger | None:
        self._ensure_loaded()
        return self._triggers.get(f"{graph_id}:{node_id}")

    def get_due(self, now: datetime | None = None) -> list[PipelineTrigger]:
        """获取所有到期的启用触发。

        cron 以 last_fired_at（无则 now）为基点求下一次；一次性和间隔
        调度按明确起点判断。间隔在每次实际触发后再等待 interval_seconds，
        避免服务卡顿或重启后突发补跑大量过期槽位。
        """
        if now is None:
            now = datetime.now(timezone.utc)
        due: list[PipelineTrigger] = []
        for t in self._triggers.values():
            if not t.enabled:
                continue
            after = _parse_iso(t.last_fired_at) if t.last_fired_at else now
            nf = t.next_fire(after)
            if nf is not None and nf <= now:
                due.append(t)
        return due

    def mark_fired(self, graph_id: str, node_id: str) -> None:
        """标记触发已发生（更新统计并落盘）。"""
        self._ensure_loaded()
        t = self._triggers.get(f"{graph_id}:{node_id}")
        if t is None:
            return
        # API 触发已占用真实 run 时，唯一计数入口是 finish_review；
        # 保留无 in-flight 的直接调用以兼容独立 store 使用方。
        if t.in_flight_run_id or t.review_enabled:
            return
        t.launch_retry_count = 0
        t.last_launch_error = ""
        t.next_launch_at = ""
        t.last_fired_at = _now_iso()
        t.run_count += 1
        if t.adaptive:
            t.next_fire_at = ""
            t.next_fire_reason = "等待本轮结果决定下次时间"
        elif t.run_at and not t.interval_seconds:
            t.one_shot_completed = True
        self._save()

    def mark_in_flight(
        self,
        graph_id: str,
        node_id: str,
        run_id: str,
    ) -> bool:
        """占用当前轮次；在完成和审查前禁止再次派发。"""
        self._ensure_loaded()
        trigger = self._triggers.get(f"{graph_id}:{node_id}")
        if trigger is None or trigger.in_flight_run_id:
            return False
        trigger.in_flight_run_id = run_id
        trigger.in_flight_phase = "running"
        trigger.attempt_count += 1
        trigger.launch_retry_count = 0
        trigger.last_launch_error = ""
        trigger.next_launch_at = ""
        if trigger.adaptive:
            trigger.next_fire_at = ""
            trigger.next_fire_reason = "本轮执行中"
        self._save()
        return True

    def mark_launch_failed(
        self,
        graph_id: str,
        node_id: str,
        error: str,
        *,
        failed_at: datetime | None = None,
    ) -> bool:
        """记录一次没有生成真实 run 的启动失败，并退避重试当前轮。"""
        self._ensure_loaded()
        trigger = self._triggers.get(f"{graph_id}:{node_id}")
        if trigger is None or trigger.in_flight_run_id:
            return False

        failed = failed_at or datetime.now(timezone.utc)
        if failed.tzinfo is None:
            failed = failed.replace(tzinfo=timezone.utc)
        trigger.launch_retry_count += 1
        delay = min(
            _MAX_LAUNCH_RETRY_DELAY,
            2 ** min(trigger.launch_retry_count, 6),
        )
        trigger.last_launch_error = (error or "触发入口未接受本轮")[:500]
        trigger.next_launch_at = _aware_iso(failed + timedelta(seconds=delay))
        trigger.next_fire_reason = (
            f"第 {trigger.launch_retry_count} 次启动未成功，{delay} 秒后重试"
        )
        self._save()
        return True

    def mark_reviewing(
        self,
        graph_id: str,
        node_id: str,
        run_id: str,
    ) -> bool:
        """把已完成的运行切到审查阶段。"""
        self._ensure_loaded()
        trigger = self._triggers.get(f"{graph_id}:{node_id}")
        if trigger is None or trigger.in_flight_run_id != run_id:
            return False
        trigger.in_flight_phase = "reviewing"
        self._save()
        return True

    def finish_review(
        self,
        graph_id: str,
        node_id: str,
        run_id: str,
        *,
        passed: bool,
        reason: str,
        reviewed_at: datetime | None = None,
    ) -> bool:
        """落盘审查结论；只有真实运行结束且 passed 才增加运行次数。"""
        self._ensure_loaded()
        trigger = self._triggers.get(f"{graph_id}:{node_id}")
        if trigger is None or trigger.in_flight_run_id != run_id:
            return False

        finished = _aware_iso(reviewed_at or datetime.now(timezone.utc))
        trigger.in_flight_run_id = ""
        trigger.in_flight_phase = ""
        trigger.last_fired_at = finished
        trigger.last_review_status = "passed" if passed else "failed"
        trigger.last_review_reason = reason[:500]
        trigger.last_reviewed_at = finished
        if passed:
            trigger.run_count += 1
            trigger.next_fire_reason = "上一轮审查通过"
        else:
            trigger.next_fire_reason = "上一轮审查未通过，将重试当前轮"

        # 一次性任务无“下一轮”；失败结论保留给用户，不自动制造无限重跑。
        if trigger.run_at and not trigger.interval_seconds and not trigger.adaptive:
            trigger.one_shot_completed = True
        self._save()
        return True

    def schedule_next(
        self,
        graph_id: str,
        node_id: str,
        next_fire_at: datetime,
        reason: str,
    ) -> bool:
        """Persist the next one-shot fire for an adaptive trigger."""
        self._ensure_loaded()
        trigger = self._triggers.get(f"{graph_id}:{node_id}")
        if trigger is None or not trigger.adaptive:
            return False
        trigger.next_fire_at = _aware_iso(next_fire_at)
        trigger.next_fire_reason = reason[:500]
        self._save()
        return True

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _save(self) -> None:
        data = [t.to_dict() for t in self._triggers.values()]
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._file_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self._file_path)
        except OSError as e:
            log.error("[pipeline-schedule] failed to save: %s", e)

    def _load(self) -> None:
        self._triggers = {}
        self._loaded = True
        if not self._file_path.exists():
            return
        try:
            data = json.loads(self._file_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("schedules.json must be a JSON array")
        except (json.JSONDecodeError, ValueError, OSError) as e:
            log.error("[pipeline-schedule] failed to load: %s", e)
            return
        for item in data:
            t = PipelineTrigger.from_dict(item)
            if t.graph_id and t.node_id:
                self._triggers[t.key] = t

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()


class PipelineScheduler:
    """后台调度循环：轮询到期触发并回调。

    与会话级 SchedulerRuntime 相互独立——pipeline 的调度内聚在
    pipeline 模块（web 进程），不依赖 TUI 进程的调度器。
    on_fire 由 api 层注入：负责 overlap 防护并发起子图 run。
    """

    def __init__(
        self,
        store: PipelineScheduleStore,
        *,
        on_fire: Callable[[PipelineTrigger], Any],
        check_interval: float = _CHECK_INTERVAL,
    ) -> None:
        self._store = store
        self._on_fire = on_fire
        self._interval = check_interval
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info(
            "[pipeline-schedule] scheduler started (interval=%.0fs)",
            self._interval,
        )

    async def shutdown(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("[pipeline-schedule] scheduler shutdown")

    async def _loop(self) -> None:
        while self._running:
            try:
                self.check_and_fire()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("[pipeline-schedule] error in check cycle")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    def check_and_fire(self, now: datetime | None = None) -> int:
        """检查到期触发并回调（同步入口，测试可直接驱动）。

        Returns:
            本轮触发的数量。
        """
        due = self._store.get_due(now)
        fired = 0
        for trigger in due:
            log.info(
                "[pipeline-schedule] firing %s (mode=%s, cron=%s, text=%s)",
                trigger.key,
                (
                    "adaptive" if trigger.adaptive
                    else "interval" if trigger.interval_seconds
                    else "once" if trigger.run_at
                    else "cron"
                ),
                trigger.cron, trigger.schedule_text[:40],
            )
            consumed = True
            failure_reason = ""
            try:
                # 仅显式返回 False 表示“本次没有真正启动”，用于有限间隔
                # 在 overlap=skip 时保留剩余次数；None 保持旧回调兼容。
                consumed = self._on_fire(trigger) is not False
            except Exception as exc:
                consumed = False
                failure_reason = str(exc) or type(exc).__name__
                log.exception(
                    "[pipeline-schedule] on_fire failed for %s", trigger.key
                )
            if consumed:
                self._store.mark_fired(trigger.graph_id, trigger.node_id)
                fired += 1
            else:
                self._store.mark_launch_failed(
                    trigger.graph_id,
                    trigger.node_id,
                    failure_reason or "触发入口暂未接受本轮",
                    failed_at=now,
                )
        return fired
