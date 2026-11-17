"""User-authored run mailbox. Delivery is recorded, never treated as approval."""
import re
import time
from mewcode.orchestration.errors import OrchestrationError

MARKER = "[MEWCODE_RUN_INPUT:"


def enqueue_intervention(snapshot, identity, text, *, kind="supplement"):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identity):
        raise OrchestrationError("无效的补充消息 ID")
    text = text.strip()
    if not text or len(text) > 8000:
        raise OrchestrationError("补充消息应为 1～8000 字")
    messages = snapshot.setdefault("interventions", [])
    previous = next((item for item in messages if item["id"] == identity), None)
    if previous is not None:
        if previous["text"] != text or previous.get("kind", "supplement") != kind:
            raise OrchestrationError("消息 ID 已用于不同内容")
        return False
    if snapshot.get("status") != "running" and not (kind == "takeover" and snapshot.get("status") == "paused"):
        raise OrchestrationError("当前编排已停止；请在对话中继续后再补充")
    if len(messages) >= 32:
        raise OrchestrationError("本次编排已接收 32 条补充；请暂停后整理目标并继续")
    messages.append({"id": identity, "text": text, "kind": kind, "status": "queued",
                     "created_at": time.time(), "delivered_to": []})
    from mewcode.experts.authorization_ledger import record_authorization
    statement = record_authorization(snapshot, kind='user_statement', scope={}, quote=text,
        source_ref=identity)
    messages[-1]['statement_id'] = statement['auth_id']
    update_receipts(snapshot)
    return True


def update_receipts(snapshot):
    snapshot["intervention_receipts"] = [
        {key: item[key] for key in ("id", "created_at", "delivered_to", "kind", "status", "decision", "target_node_id") if key in item}
        for item in snapshot.get("interventions") or []
    ]


def has_pending_interventions(snapshot, conversation):
    """Historical or takeover messages do not require a delivery transaction.

    Conversation markers remain authoritative: a new/resumed conversation
    without its marker still receives the message even after a saved receipt.
    Legacy statements without their authorization record need one real write.
    """
    history = "\n".join(str(message.content) for message in conversation.history if message.role == "user")
    return any(item.get('kind') != 'takeover' and (
        not item.get('statement_id') or f"{MARKER}{item['id']}]" not in history)
        for item in snapshot.get('interventions') or [])


def deliver_interventions(snapshot, conversation, *, node_id, node_attempt, member_id=""):
    delivered = []
    history = "\n".join(str(message.content) for message in conversation.history if message.role == "user")
    for item in snapshot.get("interventions") or []:
        if item.get("kind") == "takeover":
            # A side task belongs to the main agent, never every worker node.
            continue
        if not item.get('statement_id'):
            from mewcode.experts.authorization_ledger import record_authorization
            item['statement_id'] = record_authorization(snapshot, kind='user_statement', scope={},
                quote=item['text'], source_ref=item['id'])['auth_id']
        marker = f"{MARKER}{item['id']}]"
        if marker in history:
            continue
        conversation.add_system_reminder(
            marker + "\n用户在本次编排运行中补充：\n" + item["text"]
            + "\n请在当前节点职责内处理。不自动授权文件、命令或外发操作；"
              "不把此消息当成审批答复、验收通过或修改冻结验收项。涉及这些变更时走原审批/修订入口。"
            + f"已记录为用户表态 {item.get('statement_id', '')}；需要执行时宿主会请你一键确认，后续授权块可读取此表态。")
        target = {"node_id": node_id, "node_attempt": node_attempt, "member_id": member_id}
        if target not in item["delivered_to"]:
            item["delivered_to"].append(target)
        delivered.append(item["id"])
    update_receipts(snapshot)
    return delivered
