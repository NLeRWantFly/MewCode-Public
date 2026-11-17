"""Read-only, bounded lifecycle facts shared by the model and recovery panel.

No claims, commands, registry writes, cleanup or authority changes happen here.
Evidence observations never settle a transaction or establish command success.
"""
from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import re

from .transaction_registry import TransactionRegistry, private_candidate_only


def command_fingerprint(command: str) -> str:
    return hashlib.sha256(command.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()


def pending_command_context(workspace: Path, command: str) -> dict:
    """Keep an unknown isolated operation out of the host runner's retry path.

    Independent work is reviewed against these facts. The old record is never
    settled, and private request scripts are neither returned nor logged.
    """
    from .lpac_runtime import MAX_REQUEST, checked_path
    records = TransactionRegistry().pending(workspace)
    items, replay = [], False
    candidate = str(command or "").replace("\r\n", "\n").strip()
    for record in records:
        if not private_candidate_only(record):
            continue  # The project write fence handles these records.
        matched = False
        known = bool(record.get("command_sha256"))
        if known:
            matched = record["command_sha256"] == command_fingerprint(candidate)
        elif record.get("request_id") and record.get("request_sha256"):
            try:
                path = checked_path(Path(record["transaction"]) / "cleanup/request.bin", file=True)
                with path.open("rb") as stream:
                    raw = stream.read(MAX_REQUEST + 1)
                if len(raw) > MAX_REQUEST or hashlib.sha256(raw).hexdigest() != record["request_sha256"]:
                    raise ValueError("Unbound frozen request")
                envelope = json.loads(raw)
                request = envelope["request"]
                if (envelope["request_id"] != record["request_id"]
                        or Path(envelope["original_workspace"]).resolve() != workspace.resolve()):
                    raise ValueError("Frozen request identity differs")
                arguments = request["arguments"]
                if arguments[-2] != "-Command" or not isinstance(arguments[-1], str):
                    raise ValueError("Unknown command encoding")
                script = arguments[-1].replace("\r\n", "\n").strip()
                # Older records contain the exact script after a host bootstrap.
                # RunVerification additionally appends the exit-code trailer.
                scripts = (script, script.removesuffix("; exit $LASTEXITCODE").rstrip())
                matched = bool(candidate) and any(value.endswith(candidate) for value in scripts)
                known = True
            except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError):
                known = False
        replay |= matched
        items.append({"id": record["id"], "state": record["state"],
                      "isolated_candidate_only": True, "original_command_known": known,
                      "matches_original_command": matched,
                      "instruction": "旧结果未知，不得据此宣称完成；仅推进独立的新操作。"})
    return {"items": items[:20], "replays_unknown": replay}


def transaction_summary(record: dict, *, task_id: str | None = None) -> dict:
    native = record.get("native_receipt") or {}
    confirmed = native.get("cleanup_confirmed") is True
    evidence = "host_receipt" if confirmed else "unbound"
    if not confirmed and record.get("transaction") and record.get("request_id") and record.get("request_sha256"):
        from .cleanup_evidence import read_cleanup_evidence
        from .lpac_runtime import checked_path
        try:
            directory = checked_path(Path(record["transaction"]))
            proof_path = directory / "cleanup/receipt.json"
            if not os.path.lexists(proof_path):
                evidence = "missing"
            else:
                read_cleanup_evidence(proof_path, request_id=record["request_id"],
                                      request_sha256=record["request_sha256"])
                evidence = "verified"
        except (OSError, ValueError, RuntimeError):
            evidence = "invalid_or_unreadable"

    state = record["state"]
    if state == "preparing":
        code, message = "preparation_interrupted", "事务停在准备阶段，需宿主独占核验后收尾。"
        next_step = "使用现有恢复入口检查；独占核验前不能认定进程已结束。"
    elif confirmed or evidence == "verified":
        code, message = "reconcile_pending", "已有清理凭证，事务仍待宿主结算。"
        next_step = "使用现有恢复入口核验并结算；清理完成不代表原命令成功。"
    elif state == "executing":
        code, message = "execution_unconfirmed", "事务登记为执行中，尚无有效的结束与清理凭证。"
        next_step = "由宿主核验执行所有权和清理结果；不能仅根据经过时间或进程 ID 判断结束。"
    else:
        code = "cleanup_evidence_missing" if evidence == "missing" else "cleanup_evidence_unverified"
        message = ("旧隔离命令缺少清理凭证，当前恢复入口无法确认收尾。" if evidence == "missing" else
                   "旧隔离命令的清理凭证缺失、不可读或未通过请求身份核验。")
        next_step = "旧隔离执行保持未确认，不重放原命令。Auto 可按当前权限审查独立的新操作；重复确认业务方案或反复点击恢复不能补齐凭证。"
    reason = record.get("reason")
    result = {"id": record["id"], "task_id": record["task_id"], "state": state,
              "private_only": private_candidate_only(record), "cleanup_confirmed": native.get("cleanup_confirmed"),
              "execution_receipt_present": bool(native), "cleanup_evidence": evidence,
              "command_started": native.get("command_started") if type(native.get("command_started")) is bool else None,
              "diagnostic_code": code, "message": message, "next_step": next_step,
              "failure_type": reason if isinstance(reason, str) and re.fullmatch(r"[A-Za-z_]{1,80}", reason) else None}
    if task_id is not None:
        result["current_task"] = record["task_id"] == task_id
    return result


def workspace_transactions(workspace: Path) -> dict:
    try:
        records = TransactionRegistry().pending(workspace)
        return {"available": True, "read_only": True, "total": len(records),
                "truncated": len(records) > 20,
                "items": [transaction_summary(record) for record in records[:20]],
                "guidance": "这些是宿主直接读取的当前事务事实，无需再让用户截图或抄录同一面板。"
                            "有清理证据缺口时保留旧执行的待验证项；独立新操作以当前执行后端和权限为准。不要反复运行 RuntimeDoctor recover，"
                            "它只恢复执行环境启用状态，不会补齐清理凭证。"}
    except (OSError, ValueError, RuntimeError) as error:
        return {"available": False, "read_only": True, "error_type": type(error).__name__,
                "guidance": "宿主事务诊断读取失败，不能将未知状态视为没有未结事务。"}
