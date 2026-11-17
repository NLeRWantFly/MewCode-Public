"""Host checks of fixed delivery bytes; reports never grant tool permissions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mewcode.kernel.events import digest
from mewcode.kernel.contracts import blocking_check, handoff_findings_error


def execution_fault(contract, report):
    """Advisory check defects may wait; model outages and clearances cannot."""
    from mewcode.kernel.collaboration_faults import report_fault
    advisory_faults = {'review': {'acceptance_format'},
                       'command': {'environment_fault'}, 'test': {'environment_fault'}}
    optional = {row['id']: advisory_faults.get(row['kind'], set())
                for row in contract['checks'] if not blocking_check(row)}
    checks = [row for row in report.get('checks', [])
              if (row.get('evidence') or {}).get('execution_error') not in optional.get(row.get('id'), ())]
    return report_fault({**report, 'checks': checks})


def report_ready(contract, report):
    """Validate a host handoff separately from a fully passing quality report."""
    if execution_fault(contract, report):
        return False
    if 'ready' not in report:
        return report.get("passed") is True  # Historical fully checked receipts.
    if (report.get("ready") is not True or report.get("execution_error")
            or report.get("artifacts_unchanged") is not True):
        return False
    checks = report.get("checks", [])
    required = contract["checks"]
    if (len(checks) != len(required)
            or {(c.get("id"), c.get("version")) for c in checks}
            != {(c["id"], c["version"]) for c in required}):
        return False
    actual = {row["id"]: row for row in checks}
    return (bool(report.get("artifacts")) or any(row["kind"] == "gate" for row in required)) and all(
        bool(actual[row["id"]].get("evidence"))
        and (not blocking_check(row) or actual[row["id"]].get("passed") is True)
        for row in required)


def review_node_id(node_id, attempt_id, acceptance_round, check_id):
    return node_id + "__acceptance_" + digest([attempt_id, acceptance_round, check_id])[:12]


def review_binding(context, check):
    """Host ownership is saved before a review can be interrupted."""
    return {"node_id": context["node_id"], "attempt_id": context["attempt_id"],
            "acceptance_round": context.get("acceptance_round", 0), "check_id": check["id"],
            "check_hash": digest(check),
            "recovery_node_id": review_node_id(context["node_id"], context["attempt_id"],
                context.get("acceptance_round", 0), check["id"])}


def paused_review_binding(record, identity, call):
    """Prove a dispatched review belongs to an unchanged, paused delivery.

    Older hosts saved no binding until a review returned. For those records,
    only inline deliveries can be proven without reading mutable files.
    """
    from mewcode.kernel import collaboration as ledger

    if call.get("kind") != "acceptance" or call.get("status") != "dispatched" or call.get("result") is not None:
        return None
    value = record.get("collaboration") or {}
    stages = record.get("node_states") or record.get("stages") or {}
    for attempt in value.get("attempts", {}).values():
        if attempt.get("status") != "checking" or attempt.get("superseded"):
            continue
        try:
            ledger.check_attempt(value, attempt["id"])
            contract = value["contracts"][attempt["contract_id"]]
            delivery = attempt.get("delivery") or {}
            if delivery.get("status") != "completed":
                continue
            handoff = delivery.get("handoff") or {}
            context = {"node_id": attempt["contract_id"], "attempt_id": attempt["id"],
                       "acceptance_round": value.get("acceptance_rounds", {}).get(attempt["id"], 0)}
            for check in contract["checks"]:
                if check.get("kind") != "review":
                    continue
                binding = review_binding(context, check)
                stage = stages.get(binding["recovery_node_id"]) or {}
                if (stage.get("status") != "paused" or stage.get("role") != "auditor"
                        or stage.get("input_revision") != attempt["dependencies"]):
                    continue
                if "binding" in call:
                    expected = {**binding, "call_id": identity, "handoff_hash": digest(handoff)}
                    if call["binding"] == expected:
                        return expected
                    continue
                if handoff.get("status") not in {None, "completed"} or handoff.get("artifacts"):
                    continue
                artifacts = [{"kind": "inline", "name": row["name"], "content": row["content"],
                              "sha256": hashlib.sha256(row["content"].encode("utf-8")).hexdigest()}
                             for row in handoff.get("inline_outputs", [])]
                if artifacts and identity == digest(["acceptance", attempt["id"],
                        context["acceptance_round"], check, artifacts]):
                    return {**binding, "call_id": identity, "handoff_hash": digest(handoff)}
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return None


def retry_malformed_review(record, attempt):
    """An explicit Continue retries a finished malformed review, not its producer."""
    from mewcode.kernel import collaboration as ledger
    from mewcode.kernel.collaboration_faults import report_fault
    from mewcode.kernel.collaboration_recovery import unresolved_calls

    report = attempt.get('acceptance_fault') or {}
    delivery = attempt.get('delivery') or {}
    if (attempt.get('status') != 'checking' or delivery.get('status') != 'completed'
            or report_fault(report) != 'acceptance_format'
            or record.get('attempt', 1) <= attempt.get('acceptance_fault_host_attempt', attempt.get('run_attempt', 1))
            or unresolved_calls(record, [attempt['contract_id']])):
        return
    value = ledger.state(record)
    try:
        ledger.check_attempt(value, attempt['id'])
    except ValueError:
        return
    current_round = value.get('acceptance_rounds', {}).get(attempt['id'], 0)
    context = dict(node_id=attempt['contract_id'], attempt_id=attempt['id'], acceptance_round=current_round)
    retry = False
    for check in value['contracts'][attempt['contract_id']]['checks']:
        if check.get('kind') != 'review':
            continue
        identity = digest(['acceptance', attempt['id'], current_round, check, report.get('artifacts', [])])
        call = value['calls'].get(identity) or {}
        if call.get('status') in {'waiting', 'dispatched'}:
            return  # A paused/uncertain review retains its original identity.
        expected = {**review_binding(context, check), 'call_id': identity,
                    'handoff_hash': digest(delivery.get('handoff') or {})}
        if (call.get('kind') == 'acceptance' and call.get('status') == 'finished'
                and call.get('binding') == expected
                and (call.get('result') or {}).get('execution_error') == 'acceptance_format'):
            retry = True
    if retry:
        value.setdefault('acceptance_rounds', {})[attempt['id']] = current_round + 1
        ledger.event(value, 'acceptance_retry_on_resume', attempt_id=attempt['id'],
                     previous_round=current_round, run_attempt=record['attempt'])


class Acceptance:
    def __init__(self, work_dir, *, review=None, command=None):
        self.work_dir = Path(work_dir).resolve()
        self.review, self.command = review, command

    def _path(self, name):
        path = (self.work_dir / name).resolve()
        if not path.is_relative_to(self.work_dir):
            raise ValueError("验收产物超出工作区")
        return path

    def freeze(self, handoff):
        artifacts = []
        for row in handoff.get("artifacts", []):
            path = self._path(row["path"])
            content = path.read_bytes()
            fingerprint = hashlib.sha256(content).hexdigest()
            snapshot = self._path(".mewcode/collaboration/artifacts/" + fingerprint)
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            try:
                with snapshot.open("xb") as output:
                    output.write(content)
            except FileExistsError:
                if hashlib.sha256(snapshot.read_bytes()).hexdigest() != fingerprint:
                    raise ValueError("固定产物存储损坏，不能采用该版本")
            artifacts.append({"kind": "file", "path": str(path.relative_to(self.work_dir)),
                              "snapshot_path": str(snapshot.relative_to(self.work_dir)),
                              "sha256": fingerprint, "size": len(content)})
        for row in handoff.get("inline_outputs", []):
            artifacts.append({"kind": "inline", "name": row["name"], "content": row["content"],
                              "sha256": hashlib.sha256(row["content"].encode("utf-8")).hexdigest()})
        return artifacts

    def unchanged(self, artifacts):
        try:
            return all(row["sha256"] == hashlib.sha256(self._path(row["path"]).read_bytes()).hexdigest()
                       and row["sha256"] == hashlib.sha256(self._path(row.get("snapshot_path", row["path"])).read_bytes()).hexdigest()
                       for row in artifacts if row["kind"] == "file")
        except (OSError, ValueError):
            return False

    async def check(self, contract, result, *, context):
        handoff = result.handoff or {}
        if result.status != "completed" or handoff.get("status") not in {None, "completed"}:
            return {"passed": False, "checks": [], "artifacts": [], "reason": "工作单元尚未完成"}
        findings_error = handoff_findings_error(handoff, contract['checks'])
        if findings_error:
            return {"passed": False, "checks": [], "artifacts": [], "reason": findings_error}
        if contract.get("method") and (handoff.get("method_id") != contract["method"]["id"]
                or (handoff.get("method_decision") or None) != contract.get("method_decision")):
            return {"passed": False, "checks": [], "artifacts": [], "reason": "指定方法缺少有效决定引用"}
        try:
            artifacts = self.freeze(handoff)
        except (OSError, ValueError, KeyError) as exc:
            return {"passed": False, "checks": [], "artifacts": [], "reason": "产物无法固定：" + str(exc)}
        checks = []
        for check in contract["checks"]:
            kind = check["kind"]
            evidence, passed = {}, False
            if kind == "review" and not blocking_check(check) and context.get("phase") == "handoff":
                checks.append({"id": check["id"], "version": check["version"], "passed": False,
                    "deferred": True, "evidence": {"reason": "质量检查留待集中复核；当前仅确认产物可交接。"}})
                continue
            try:
                if kind == "file_exists":
                    path = self._path(check["path"])
                    passed = path.is_file() and any(a.get("path") == str(path.relative_to(self.work_dir)) for a in artifacts)
                    evidence = {"path": check["path"]}
                elif kind == "schema":
                    from jsonschema import validate
                    path = self._path(check["path"])
                    if not any(a.get("path") == str(path.relative_to(self.work_dir)) for a in artifacts):
                        raise ValueError("schema 检查必须绑定已固定产物")
                    fixed = next(a for a in artifacts if a.get("path") == str(path.relative_to(self.work_dir)))
                    validate(json.loads(self._path(fixed["snapshot_path"]).read_text(encoding="utf-8")), check["schema"])
                    passed, evidence = True, {"path": check["path"], "schema_hash": digest(check["schema"])}
                elif kind in {"command", "test"}:
                    if self.command is None:
                        raise ValueError("宿主命令验收通道不可用，需保留阻塞")
                    evidence = await self.command(check, context)
                    passed = (type(evidence.get("exit_code")) is int and evidence["exit_code"] == 0
                              and evidence.get("is_error") is not True and evidence.get("command_started") is not False
                              and bool(evidence.get("receipt_id")))
                elif kind == "review":
                    if self.review is None:
                        raise ValueError("独立验收模型不可用，需要人工处理")
                    evidence = await self.review({"contract": contract, "check": check, "artifacts": artifacts,
                                                  "unverified_report": result.text, "handoff": handoff}, context)
                    passed = evidence.get("passed") is True and bool(evidence.get("evidence"))
                elif kind == "gate":
                    passed = context.get("gate_confirmed") is True
                    evidence = {"host_decision": passed}
                else:
                    raise ValueError("未知验收检查类型")
            except Exception as exc:
                from mewcode.experts.waits import BranchWaitPaused
                evidence = {"error": str(exc), "kind": type(exc).__name__}
                if isinstance(exc, BranchWaitPaused):
                    evidence.update(execution_error="user_input", reason=exc.reason,
                        recovery_node_id=exc.node_id, request_id=exc.request_id)
                elif kind in {"review", "command", "test"}:
                    evidence["execution_error"] = "acceptance_unavailable" if kind == "review" else "environment_fault"
            checks.append({"id": check["id"], "version": check["version"],
                           "passed": passed and not bool(evidence.get('execution_error')), "evidence": evidence})
            from mewcode.kernel.collaboration_faults import REQUIRES_CLEARANCE
            if evidence.get("execution_error") in REQUIRES_CLEARANCE:
                break
        unchanged = self.unchanged(artifacts)
        substantive = any(c["kind"] in {"schema", "command", "test", "review", "gate"} for c in contract["checks"])
        # Advisory findings do not block handoff. An unavailable selected model
        # or missing host clearance remains an execution prerequisite.
        report = {"passed": unchanged and substantive and bool(checks) and all(c["passed"] for c in checks),
                "artifacts": artifacts, "checks": checks,
                 "execution_error": execution_fault(contract, {"checks": checks}),
                "artifacts_unchanged": unchanged,
                "reason": "" if unchanged else "验收期间产物已变化，旧报告不能交接"}
        report["ready"] = unchanged and not report["execution_error"]
        report["ready"] = report_ready(contract, report)
        if not report['ready'] and not report['reason'] and not artifacts and not any(c['kind'] == 'gate' for c in contract['checks']):
            report['reason'] = '交接缺少可回读的文件或内联产物'
        return report
