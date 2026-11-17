"""Small, evidence-based retry memory independent of the model transcript."""
from dataclasses import asdict, is_dataclass
import json
import re

VERIFICATION_TOOLS = {"Bash", "RunVerification", "VerifyCandidate"}


def compact_tool_output(name, output):
    """Keep failure tails and structured candidate proof, not just log headers."""
    output = str(output or "")
    if name == "VerifyCandidate":
        try:
            data = json.loads(output)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            result = {key: data[key] for key in ("runner", "targets", "cwd", "python", "dependency_mode",
                      "exit_code", "source_unchanged", "source_check_scope", "artifact_hash") if key in data}
            result.update(changed_files=list(data.get("changed_files") or [])[:20],
                          output=str(data.get("output") or "")[-3000:], diff=str(data.get("diff") or "")[:2000])
            return json.dumps(result, ensure_ascii=False)
    if len(output) <= 4000:
        return output
    return output[:1000] + "\n[中间输出已省略]\n" + output[-2900:]


def collect_verification_records(tools):
    # A repeated allowlisted probe reuses its host result. Keep distinct commands
    # so repeated result reads cannot evict another requested command's evidence.
    unique = {}
    for tool_id, item in tools.items():
        if item.get("name") not in VERIFICATION_TOOLS:
            continue
        key = ("Bash", str((item.get("args") or {}).get("command"))) if item.get("name") == "Bash" else (tool_id,)
        prior = unique.get(key)
        prior_host = ((prior[1].get("host_observation") or {}).get("command_execution") or {}) if prior else {}
        current_host = ((item.get("host_observation") or {}).get("command_execution") or {})
        if prior_host.get("status") == "exited" and current_host.get("status") != "exited":
            continue
        current_unstarted = (item.get("status") in {"queued", "waiting_permission", "interrupted_before_execution"}
                             or current_host.get("command_started") is False)
        prior_may_have_started = (prior and prior[1].get("status") not in {
            "queued", "waiting_permission", "interrupted_before_execution"}
            and prior_host.get("command_started") is not False)
        if current_unstarted and prior_may_have_started:
            continue  # An unstarted retry cannot erase an earlier unknown effect.
        unique[key] = (tool_id, item)
    return [{"tool_id": tool_id, "name": item.get("name"), "args": item.get("args"),
             "status": item.get("status"), "is_error": bool(item.get("is_error")),
             "host_observation": item.get("host_observation"),
             "output_truncated": item.get("output_truncated", False),
             "output": compact_tool_output(item.get("name"), item.get("output"))}
            for tool_id, item in unique.values()][-8:]


def summarize_attempt(value, *, attempt=0):
    data = asdict(value) if is_dataclass(value) else dict(value or {})
    projected = data.get("ledger_view")
    if isinstance(projected, dict):
        handoff = (projected.get("last_handoff") or {}).get("payload") or {}
        return {"attempt": attempt or data.get("node_attempt", 0), "epoch": projected["epoch"],
                "status": projected["phase"], "summary": str(handoff.get("summary") or "")[:1200],
                "failed_commands": [], "changed_files": [],
                "artifacts": [str(f["payload"].get("path") or "") for f in projected.get("artifacts", [])][-12:],
                "open_items": list(handoff.get("open_items") or [])[:8]}
    records = list(data.get("verification_records") or [])
    tools = data.get("tools") or {}
    records.extend({**item, "tool_id": key} for key, item in tools.items()
                   if isinstance(item, dict) and item.get("name") in VERIFICATION_TOOLS)
    failures, seen, verified_changes = [], set(), []
    for item in records:
        if not isinstance(item, dict):
            continue
        output = str(item.get("output") or "")
        proof = {}
        if item.get("name") == "VerifyCandidate":
            try:
                decoded = json.loads(output)
                proof = decoded if isinstance(decoded, dict) else {}
            except ValueError:
                pass
            verified_changes.extend(proof.get("changed_files") or [])
        exit_match = re.search(r"(?:EXIT CODE:|退出码)\s*(-?\d+)", output, re.I)
        failed = (item.get("is_error") or item.get("ok") is False or item.get("status") in {"failed", "stalled"}
                  or proof.get("exit_code", 0) not in {0, None} or proof.get("source_unchanged") is False
                  or (exit_match and int(exit_match[1]) != 0))
        if not failed:
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        command = str(item.get("command") or args.get("command")
                      or (json.dumps(args, ensure_ascii=False) if args else item.get("name") or ""))
        output = str(proof.get("output") or output)
        signature = (command, output)
        if signature in seen:
            continue
        seen.add(signature)
        failures.append({"command": command[:800], "output": output[-1200:], "status": str(item.get("status") or "failed")})
    changed = list(data.get("changed_files") or []) + verified_changes
    changed.extend(item.get("path") for item in data.get("activity_records") or []
                   if isinstance(item, dict) and item.get("kind") == "edit" and item.get("path"))
    changed.extend((item.get("args") or {}).get("file_path") or (item.get("args") or {}).get("path")
                   for item in tools.values() if isinstance(item, dict) and item.get("name") in {"WriteFile", "EditFile"}
                   and item.get("status") == "completed")
    handoff = data.get("handoff") or {}
    return {"attempt": attempt or data.get("node_attempt", 0), "status": str(data.get("status") or "unknown"),
            "summary": str(handoff.get("summary") or "")[:1200],
            "failed_commands": failures[-3:],
            "changed_files": list(dict.fromkeys(str(path)[:500] for path in changed if path))[-20:],
            "artifacts": [str(item.get("path") or "")[:500] for item in (handoff.get("artifacts") or [])[:12] if isinstance(item, dict)],
            "open_items": [str(item)[:300] for item in (handoff.get("open_items") or [])[:8]]}


def attempt_prompt(history, *, attempt):
    if not history:
        return ""
    return (f"\n\n【第 {attempt} 次尝试 · 上次进度】\n"
            "以下是当前代际最近一段的结构化交接。核验已有成果和未知写入的效果，"
            "依据 open_items 继续；缺少能力时通过 RequestAssistance 请求主模型协助。\n"
            + json.dumps(history[-1:], ensure_ascii=False))


def checkpoint_attempt_histories(checkpoint):
    """Recover bounded memory even from legacy acceptance resets.

    Archives are evidence, never runnable state: do not restore messages,
    approvals, completed status, or old output artifacts as current results.
    """
    if "node_ledgers" in checkpoint:
        from mewcode.experts.node_ledger import project_node_view
        result = {}
        for node_id in checkpoint["node_ledgers"]:
            project_node_view(checkpoint, node_id)
            stage = checkpoint["node_states"][node_id]
            result[node_id] = [summarize_attempt(stage)]
        return result
    histories = {}
    buckets = [item.get("previous") or {} for item in (checkpoint.get("acceptance_rechecks") or [])[-3:]
               if isinstance(item, dict)]
    buckets.append(checkpoint.get("node_states") or checkpoint.get("stages") or {})
    for bucket in buckets:
        for node_id, state in bucket.items():
            if not isinstance(state, dict):
                continue
            entries = state.get("attempt_history") or (
                [summarize_attempt(state)] if any(state.get(key) for key in
                    ("text", "partial_text", "transcript", "tools", "verification_records")) else [])
            history = histories.setdefault(node_id, [])
            for entry in entries:
                if isinstance(entry, dict) and entry not in history:
                    history.append(entry)
            del history[:-3]
    return histories
