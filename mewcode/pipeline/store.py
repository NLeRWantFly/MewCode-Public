"""Pipeline 图的持久化存储。

图定义存储在 .mewcode/pipelines/*.json
执行记录存储在 .mewcode/pipelines/runs/*.jsonl
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mewcode.pipeline.models import (
    PipelineGraph,
    PipelineListItem,
    PipelineRunRecord,
)

log = logging.getLogger(__name__)

PIPELINES_DIR = ".mewcode/pipelines"
RUNS_DIR = "runs"
_RESERVED_PIPELINE_FILES = {"schedules.json"}
_ARTIFACT_EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp",
}



class VersionConflictError(Exception):
    """乐观锁版本冲突：客户端持有的版本与存储中的当前版本不一致。

    携带 current_version 供 API 层放进 409 响应，客户端据此提示
    「刷新加载最新版」。
    """

    def __init__(self, current_version: int) -> None:
        super().__init__(
            f"Version conflict: current version is {current_version}"
        )
        self.current_version = current_version


class PipelineStore:
    """Pipeline 图和执行记录的持久化管理器。"""

    def __init__(self, work_dir: str) -> None:
        self._work_dir = Path(work_dir)
        self._pipelines_dir = self._work_dir / PIPELINES_DIR
        self._runs_dir = self._pipelines_dir / RUNS_DIR

    def _ensure_dirs(self) -> None:
        """确保目录存在。"""
        self._pipelines_dir.mkdir(parents=True, exist_ok=True)
        self._runs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Pipeline CRUD
    # ------------------------------------------------------------------

    def list_pipelines(self) -> list[PipelineListItem]:
        """列出所有 pipeline。"""
        items: list[PipelineListItem] = []

        for json_file in sorted(self._pipelines_dir.glob("*.json")):
            if json_file.name in _RESERVED_PIPELINE_FILES:
                continue
            try:
                graph = self._load_graph_file(json_file)
                if graph:
                    items.append(
                        PipelineListItem(
                            graph_id=graph.graph_id,
                            name=graph.name,
                            description=graph.description,
                            version=graph.version,
                            node_count=len(graph.nodes),
                            created_at=graph.created_at,
                            updated_at=graph.updated_at,
                        )
                    )
            except Exception as e:
                log.warning("Failed to load pipeline %s: %s", json_file.name, e)

        return items

    def get_pipeline(self, graph_id: str) -> PipelineGraph | None:
        """获取指定 pipeline。"""
        json_file = self._pipelines_dir / f"{graph_id}.json"
        if not json_file.exists():
            return None
        return self._load_graph_file(json_file)

    def create_pipeline(self, graph: PipelineGraph) -> PipelineGraph:
        """创建新 pipeline。"""
        self._ensure_dirs()
        now = datetime.now(timezone.utc).isoformat()
        graph.created_at = now
        graph.updated_at = now

        json_file = self._pipelines_dir / f"{graph.graph_id}.json"
        self._save_graph_file(json_file, graph)
        log.info("Created pipeline '%s' (%s)", graph.name, graph.graph_id)
        return graph

    def update_pipeline(
        self,
        graph: PipelineGraph,
        *,
        expected_version: int | None = None,
    ) -> PipelineGraph | None:
        """更新 pipeline。

        Args:
            graph: 要保存的图（version 为客户端加载时的值）。
            expected_version: 乐观锁：客户端加载图时的 version。提供时在
                version 自动 +1 之前与存储中的当前版本比对，不一致抛
                VersionConflictError（含 current_version）；None = 跳过
                比对（兼容不带版本号的旧客户端）。

        Raises:
            VersionConflictError: expected_version 与存储中的版本不一致。
        """
        self._ensure_dirs()
        json_file = self._pipelines_dir / f"{graph.graph_id}.json"
        if not json_file.exists():
            return None

        if expected_version is not None:
            current = self._load_graph_file(json_file)
            if current is not None and current.version != expected_version:
                raise VersionConflictError(current.version)
            # 以存储版本为基线递增，防止客户端传来的 version 字段漂移
            if current is not None:
                graph.version = current.version

        graph.updated_at = datetime.now(timezone.utc).isoformat()
        graph.version += 1
        self._save_graph_file(json_file, graph)
        log.info("Updated pipeline '%s' (%s)", graph.name, graph.graph_id)
        return graph

    def duplicate_pipeline(self, graph_id: str) -> PipelineGraph | None:
        """复制 pipeline：新 graph_id、名称加「(副本)」后缀、版本重置。

        与现有名称冲突时递增序号（「xx (副本 2)」「xx (副本 3)」…）。
        源不存在返回 None。
        """
        source = self.get_pipeline(graph_id)
        if source is None:
            return None

        existing_names = {item.name for item in self.list_pipelines()}
        name = f"{source.name} (副本)"
        seq = 2
        while name in existing_names:
            name = f"{source.name} (副本 {seq})"
            seq += 1

        copy = source.model_copy(deep=True)
        copy.graph_id = f"g_{uuid.uuid4().hex[:8]}"
        copy.name = name
        copy.version = 1
        return self.create_pipeline(copy)

    def import_pipeline(self, graph: PipelineGraph) -> PipelineGraph:
        """导入图定义：分配新 graph_id、版本重置后新建管线。

        始终新建（不覆盖同 graph_id 的既有管线），导出文件可反复导入。
        """
        copy = graph.model_copy(deep=True)
        copy.graph_id = f"g_{uuid.uuid4().hex[:8]}"
        copy.version = 1
        return self.create_pipeline(copy)

    def delete_pipeline(self, graph_id: str) -> bool:
        """删除 pipeline。"""
        self._ensure_dirs()
        json_file = self._pipelines_dir / f"{graph_id}.json"
        if not json_file.exists():
            return False

        json_file.unlink()
        log.info("Deleted pipeline %s", graph_id)

        # 同时删除相关的执行记录，以及每个 run 的事件流/快照文件
        runs_file = self._runs_dir / f"{graph_id}.jsonl"
        if runs_file.exists():
            for run_id in self._run_ids_in(runs_file):
                try:
                    (self._runs_dir / f"{run_id}.events.jsonl").unlink(
                        missing_ok=True
                    )
                except OSError:
                    pass
                self.delete_run_snapshot(run_id)
            runs_file.unlink()

        return True

    @staticmethod
    def _run_ids_in(runs_file: Path) -> set[str]:
        """从 runs/{graph_id}.jsonl 收集全部 run_id（解析失败的行跳过）。"""
        run_ids: set[str] = set()
        try:
            with open(runs_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rid = json.loads(line).get("run_id")
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rid, str) and rid:
                        run_ids.add(rid)
        except OSError:
            pass
        return run_ids

    # ------------------------------------------------------------------
    # 执行记录
    # ------------------------------------------------------------------

    def save_run(self, record: PipelineRunRecord) -> None:
        """保存执行记录（追加到 JSONL 文件）。"""
        self._ensure_dirs()
        runs_file = self._runs_dir / f"{record.graph_id}.jsonl"

        with open(runs_file, "a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")

    def update_run(self, record: PipelineRunRecord) -> None:
        """更新执行记录（重写整个文件中的对应记录）。"""
        self._ensure_dirs()
        runs_file = self._runs_dir / f"{record.graph_id}.jsonl"
        if not runs_file.exists():
            self.save_run(record)
            return

        # 读取所有记录
        records: list[dict[str, Any]] = []
        with open(runs_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        if data.get("run_id") == record.run_id:
                            data = json.loads(record.model_dump_json())
                        records.append(data)
                    except json.JSONDecodeError:
                        continue

        # 重写文件
        with open(runs_file, "w", encoding="utf-8") as f:
            for data in records:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def list_runs(
        self, graph_id: str, limit: int = 50
    ) -> list[PipelineRunRecord]:
        """列出 pipeline 的执行历史。"""
        self._ensure_dirs()
        runs_file = self._runs_dir / f"{graph_id}.jsonl"
        if not runs_file.exists():
            return []

        records: list[PipelineRunRecord] = []
        with open(runs_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        records.append(PipelineRunRecord.model_validate(data))
                    except Exception as e:
                        log.warning("Failed to parse run record: %s", e)

        # 按时间倒序，返回最新的 limit 条
        records.sort(key=lambda r: r.started_at or "", reverse=True)
        return records[:limit]

    def get_run(self, graph_id: str, run_id: str) -> PipelineRunRecord | None:
        """获取单次执行记录。"""
        self._ensure_dirs()
        runs_file = self._runs_dir / f"{graph_id}.jsonl"
        if not runs_file.exists():
            return None

        found: PipelineRunRecord | None = None
        with open(runs_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        if data.get("run_id") == run_id:
                            # 取最后一条匹配（同 run_id 可能被追加多次）
                            found = PipelineRunRecord.model_validate(data)
                    except Exception:
                        continue

        return found

    # ------------------------------------------------------------------
    # 事件流持久化（runs/{run_id}.events.jsonl）
    # ------------------------------------------------------------------

    def append_run_event(self, run_id: str, event: dict[str, Any]) -> None:
        """追加一行事件到 runs/{run_id}.events.jsonl。

        事件流契约：每行一个 JSON 事件，含 seq(int 递增)/ts/type/node_id
        等字段（executor._emit 统一附加 seq/ts）。作为验收与回放的观测
        载体——例如统计某节点的 node_start 次数、核对 resolved_model。
        """
        self._ensure_dirs()
        path = self._runs_dir / f"{run_id}.events.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def load_run_events(self, run_id: str) -> list[dict[str, Any]]:
        """读取一次运行的完整事件流（文件不存在返回空列表）。"""
        path = self._runs_dir / f"{run_id}.events.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events


    def save_run_artifact(
        self, run_id: str, node_id: str, block: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist a tool-produced image outside the event stream."""
        if Path(run_id).name != run_id:
            raise ValueError("invalid run artifact path")
        mime = str(block.get("mime_type") or block.get("mime") or "").lower()
        extension = _ARTIFACT_EXTENSIONS.get(mime)
        if extension is None:
            raise ValueError(f"unsupported artifact image type: {mime}")
        encoded = block.get("data")
        if not isinstance(encoded, str):
            raise ValueError("artifact image has no base64 data")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("artifact image is not valid base64") from exc
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("artifact image exceeds 10 MiB limit")
        safe_node = "".join(
            char if char.isalnum() or char in "-_" else "_" for char in node_id
        )[:48]
        digest = hashlib.sha256(data).hexdigest()
        name = f"{safe_node or 'node'}_{digest[:16]}{extension}"
        directory = self._runs_dir / run_id / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        if not path.exists():
            path.write_bytes(data)
        return {
            "name": name, "mime": mime, "size": len(data),
            "sha256": digest,
            "url": f"/api/pipelines/run-artifacts/{run_id}/{name}",
        }

    def run_artifact_path(self, run_id: str, name: str) -> Path:
        if Path(name).name != name or Path(run_id).name != run_id:
            raise ValueError("invalid artifact path")
        directory = (self._runs_dir / run_id / "artifacts").resolve()
        path = (directory / name).resolve()
        if directory not in path.parents or not path.is_file():
            raise FileNotFoundError(name)
        return path
    # ------------------------------------------------------------------
    # 运行中快照（崩溃恢复 / 断点续跑）
    # ------------------------------------------------------------------

    @property
    def _snapshots_dir(self) -> Path:
        return self._runs_dir / "active"

    def save_run_snapshot(self, record: PipelineRunRecord) -> None:
        """把运行中的 run 完整状态写入快照文件（原子替换）。

        executor 的 on_checkpoint 每个节点完成/失败后调用一次，
        进程崩溃或被取消后可从快照恢复并断点续跑。
        """
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        path = self._snapshots_dir / f"{record.run_id}.json"
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(record.model_dump_json())
        tmp.replace(path)

    def load_run_snapshot(self, run_id: str) -> PipelineRunRecord | None:
        """读取运行快照。"""
        path = self._snapshots_dir / f"{run_id}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return PipelineRunRecord.model_validate(json.load(f))
        except Exception as e:
            log.warning("Failed to load run snapshot %s: %s", run_id, e)
            return None

    def delete_run_snapshot(self, run_id: str) -> None:
        """删除运行快照（run 正常落盘 jsonl 后调用）。"""
        try:
            (self._snapshots_dir / f"{run_id}.json").unlink(missing_ok=True)
        except OSError:
            pass

    def find_run(self, graph_id: str, run_id: str) -> PipelineRunRecord | None:
        """查找 run 记录：先查已完成的 jsonl，再查运行中快照。"""
        rec = self.get_run(graph_id, run_id)
        if rec is not None:
            return rec
        return self.load_run_snapshot(run_id)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _load_graph_file(self, path: Path) -> PipelineGraph | None:
        """从 JSON 文件加载图定义。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return PipelineGraph.model_validate(data)
        except Exception as e:
            log.warning("Failed to load graph from %s: %s", path, e)
            return None

    def _save_graph_file(self, path: Path, graph: PipelineGraph) -> None:
        """保存图定义到 JSON 文件。"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(graph.model_dump_json(indent=2))
