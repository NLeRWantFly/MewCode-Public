"""Node-attached user requirements, versioned independently of canvas layout."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from mewcode.attachments import AttachmentError, AttachmentStore, _inspect_image


def attachment_owner(graph_id):
    return hashlib.sha256(str(graph_id).encode()).hexdigest()


def attachment_store(work_dir):
    return AttachmentStore(Path(work_dir) / ".mewcode" / "constraint-assets")


def compile_constraints(annotations, node_ids):
    result = {}
    if not isinstance(annotations, list) or len(annotations) > 200:
        raise ValueError("每个画布最多保存 200 个约束或备注")
    for note in annotations:
        if not isinstance(note, dict) or note.get("mode") != "constraint":
            continue
        node_id = str(note.get("target_id") or "")
        if node_id not in node_ids:
            raise ValueError("执行约束的目标节点不存在")
        attachments = note.get("attachments") or []
        if not isinstance(attachments, list) or len(attachments) > 8:
            raise ValueError("每个约束最多 8 个附件")
        if any(not isinstance(a, dict) for a in attachments):
            raise ValueError("约束附件格式无效，请重新上传")
        text = str(note.get("text") or "")
        if len(text) > 4000:
            raise ValueError("每个约束的文字最多 4000 字")
        if not text.strip() and not attachments:
            raise ValueError("执行约束不能为空；请填写内容或切换为备注")
        clean = {"id": str(note.get("id") or ""), "text": text,
                 "attachments": [{k: a[k] for k in ("attachment_id", "name", "kind", "mime", "data_url",
                                                     "preview_text", "truncated") if k in a}
                                 for a in attachments if isinstance(a, dict)]}
        clean["version"] = hashlib.sha256(json.dumps(clean, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        result.setdefault(node_id, []).append(clean)
    return result


def resolve_constraints(flow, node_id, work_dir, *, vision_ok):
    """Materialize exactly the locked requirements for this attempt, or fail closed."""
    value = flow.as_dict() if hasattr(flow, "as_dict") else flow
    meta = value.get("meta") or {}
    notes = (meta.get("node_constraints") or {}).get(node_id) or []
    graph_id = meta.get("pipeline_graph_id") or ""
    assets = attachment_store(work_dir)
    blocks, used = [], []
    total_bytes = 0
    total_text = 0
    for note in notes:
        if note.get("text"):
            blocks.append({"type": "text", "text": "用户对本节点的执行要求：\n" + note["text"]})
        for item in note.get("attachments") or []:
            identity = str(item.get("attachment_id") or "")
            if identity:
                if not graph_id:
                    raise AttachmentError("约束附件缺少所属画布，请重新保存约束")
                ref = assets.get(attachment_owner(graph_id), identity)
                total_bytes += ref.size
                if ref.truncated:
                    raise AttachmentError(f"约束附件 {ref.name} 内容超出读取上限，请拆分后重新上传")
                resolved = assets.materialize([{"type": "attachment", "attachment_id": identity}])
            elif item.get("kind") == "image":
                # Legacy inline screenshots can be promoted to constraints.
                raw = str(item.get("data_url") or "")
                if not raw.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
                    raise AttachmentError("约束图片格式不支持，请重新上传 PNG、JPEG 或 WebP")
                try:
                    data = base64.b64decode(raw.split(",", 1)[1], validate=True)
                    mime, _, _ = _inspect_image(data)
                except (ValueError, TypeError) as exc:
                    raise AttachmentError("约束图片已损坏，请重新上传") from exc
                total_bytes += len(data)
                resolved = [{"type": "image", "mime_type": mime, "data": base64.b64encode(data).decode()}]
            elif item.get("kind") == "text" and item.get("preview_text") and not item.get("truncated"):
                resolved = [{"type": "text", "text": str(item["preview_text"])}]
            else:
                raise AttachmentError(f"约束附件 {item.get('name', '')} 只有元数据或预览，请重新上传完整文件")
            if any(b.get("type") == "image" for b in resolved) and not vision_ok:
                raise AttachmentError("当前节点模型不支持图片，不能读取此执行约束；请更换支持图片的模型或补充文字要求")
            if total_bytes > 50 * 1024 * 1024:
                raise AttachmentError("本节点约束附件总量超过 50 MiB，请减少附件")
            blocks.extend(resolved)
        total_text = sum(len(b.get("text", "")) for b in blocks if b.get("type") == "text")
        if total_text > 200000:
            raise AttachmentError("本节点要求文字与附件正文超过 20 万字，请拆分要求")
        used.append({"id": note["id"], "version": note["version"], "text": note.get("text", ""),
                     "attachments": [{k: a[k] for k in ("attachment_id", "name", "kind") if k in a}
                                     for a in note.get("attachments") or []]})
    return blocks, used
