"""Source-bound host effect reconciliation for a paused dialogue.

Only an exact successful WriteFile/EditFile call/result pair with a matching
host-captured file receipt and unchanged current bytes is automatically proved;
WriteFile may also be checked against its frozen full-text intent byte-for-byte.
Other effects remain unresolved. These receipts prove an effect, never business
correctness, independent review, or user acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from mewcode.orchestration.paused_handoff_classify import hard_review_hold
from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex
from mewcode.orchestration.paused_handoff_review import _canonical, _journal_head, _journal_lock


_SAFE_TOOL = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_FILE_WRITERS = frozenset({"WriteFile", "EditFile", "NotebookEdit", "ApplyPatch"})
_OPEN_ENDED = frozenset({"Bash", "Terminal", "TerminalInput", "Agent", "RunVerification"})
_PROVABLE_FILE_WRITERS = frozenset({"WriteFile", "EditFile"})
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
RECONCILIATION_VERSION = "paused-handoff-effect-v1"


def _safe_tool(value: Any) -> str:
    tool = str(value or "unknown")
    return tool if _SAFE_TOOL.fullmatch(tool) else "unknown"


def _calls(record: dict[str, Any]) -> list[dict[str, Any]]:
    return (record.get("calls") or []) if record.get("record_type") == "assistant" else [record]


def _evidence_route(record: dict[str, Any], tools: list[str]) -> str:
    if record.get("parse_error") or not record.get("complete_line", True):
        return "recover_exact_source_before_any_decision"
    if record.get("record_type") == "assistant":
        return "match_every_call_to_result_and_host_verified_effect"
    if any(tool in _OPEN_ENDED for tool in tools):
        return "inspect_command_effects_and_external_state"
    if any(tool in _FILE_WRITERS for tool in tools):
        return "verify_write_intent_or_registered_output_and_current_file_fingerprint"
    if record.get("is_error"):
        return "verify_error_outcome_and_absence_of_side_effects"
    return "obtain_independent_host_effect_or_review_receipt"


def _source_matches(index: PausedHandoffIndex, snapshot: dict[str, Any]) -> None:
    source = index.manifest["source"]
    if (source.get("run_id") != snapshot.get("run_id")
            or source.get("conversation_id") != snapshot.get("conversation_id")):
        raise ValueError("reconciliation source belongs to another run")
    index.verify(original=True)


def _file_receipt_is_current(receipt: dict[str, Any], call: dict[str, Any],
                             index: PausedHandoffIndex) -> bool:
    """Recheck the host receipt, declared path and current bytes, fail closed."""
    if (receipt.get("source") != "successful_write"
            or receipt.get("tool_id") != call.get("call_id")
            or receipt.get("accepted") is not False
            or not isinstance(receipt.get("size"), int)
            or isinstance(receipt.get("size"), bool)
            or not isinstance(receipt.get("sha256"), str)
            or not _DIGEST.fullmatch(receipt["sha256"])):
        return False
    raw_path = receipt.get("path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        return False
    declared = [index.manifest.get("path_map", {}).get(path_id, {}).get("original")
                for path_id in call.get("path_ids") or []]
    try:
        path = Path(raw_path).resolve(strict=True)
        if (not path.is_file() or not any(
                isinstance(item, str) and Path(item).is_absolute()
                and Path(item).resolve(strict=True) == path for item in declared)):
            return False
        identity = (f"{receipt.get('node_id')}\0{receipt.get('member_id')}\0"
                    f"{receipt.get('node_attempt')}\0{receipt.get('tool_id')}\0"
                    f"{path}\0{receipt['sha256']}")
        if hashlib.sha256(identity.encode()).hexdigest()[:32] != receipt.get("id"):
            return False
        if path.stat().st_size != receipt["size"] or receipt["size"] > 8 * 1024 * 1024:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        return digest.hexdigest() == receipt["sha256"]
    except (OSError, ValueError):
        return False


def _verified_prior_call(index: PausedHandoffIndex,
                         result: dict[str, Any]) -> bytes | None:
    """Verify a pre-anchor call by its indexed byte range and exact input."""
    refs = [item for item in index.manifest.get("prior_call_refs") or []
            if item.get("call_id") == result.get("call_id")]
    if len(refs) != 1:
        return None
    ref = refs[0]
    start, end = ref.get("start"), ref.get("end")
    if (not isinstance(start, int) or not isinstance(end, int)
            or isinstance(start, bool) or isinstance(end, bool)
            or start < 0 or not start < end <= index.manifest["source"]["start"]
            or end - start > 8 * 1024 * 1024):
        return None
    try:
        with Path(index.manifest["source"]["log_path"]).open("rb") as handle:
            handle.seek(start)
            raw = handle.read(end - start)
        if hashlib.sha256(raw).hexdigest() != ref.get("sha256"):
            return None
        value = json.loads(raw)
        blocks = [block for block in value.get("content") or []
                  if isinstance(block, dict) and block.get("type") == "tool_use"
                  and block.get("id") == result.get("call_id")]
        if len(blocks) != 1 or blocks[0].get("name") != result.get("tool"):
            return None
        arguments = blocks[0].get("input") or {}
        fingerprint = hashlib.sha256(json.dumps(
            arguments, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()
        return raw if fingerprint == result.get("input_fingerprint") else None
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def _write_intent_matches_current(index: PausedHandoffIndex,
                                  request_raw: bytes,
                                  call: dict[str, Any],
                                  result: dict[str, Any]) -> str:
    """Prove the present bytes match a host-logged successful WriteFile intent.

    This does not claim who authored the file or that its content is accepted.
    A later edit, an ambiguous call/result or any other tool stays unresolved.
    """
    try:
        request = json.loads(request_raw)
        response = json.loads(index.read(result["id"]))
        blocks = [block for block in request.get("content") or []
                  if isinstance(block, dict) and block.get("type") == "tool_use"
                  and block.get("id") == call["call_id"]]
        if (len(blocks) != 1 or blocks[0].get("name") != "WriteFile"
                or response.get("type") != "tool_result"
                or response.get("tool_use_id") != call["call_id"]
                or response.get("is_error")):
            return ""
        arguments = blocks[0].get("input") or {}
        if not isinstance(arguments, dict):
            return ""
        fingerprint = hashlib.sha256(json.dumps(
            arguments, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()
        raw_path, content = arguments.get("file_path"), arguments.get("content")
        if (fingerprint != call.get("input_fingerprint")
                or not isinstance(raw_path, str) or not Path(raw_path).is_absolute()
                or not isinstance(content, str)):
            return ""
        output = response.get("content")
        if isinstance(output, list):
            output = "\n".join(str(block.get("text") or "") for block in output
                               if isinstance(block, dict) and block.get("type") == "text")
        if not isinstance(output, str) or not output.startswith("Successfully wrote to "):
            return ""
        expected = content.encode("utf-8")
        if len(expected) > 8 * 1024 * 1024:
            return ""
        path = Path(raw_path).resolve(strict=True)
        if not path.is_file() or path.stat().st_size != len(expected):
            return ""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        expected_sha = hashlib.sha256(expected).hexdigest()
        return expected_sha if digest.hexdigest() == expected_sha else ""
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return ""


def _prove_record(index: PausedHandoffIndex, snapshot: dict[str, Any],
                  record: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Return deterministic evidence for one record, never a semantic verdict."""
    reason = hard_review_hold(record)
    if not reason:
        return None, "not_a_hard_hold"
    if record.get("parse_error") or not record.get("complete_line", True):
        return None, "source_unparseable"
    all_calls: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    all_results: dict[str, list[dict[str, Any]]] = {}
    for item in index.manifest["records"]:
        for call in item.get("calls") or []:
            all_calls.setdefault(call.get("call_id") or "", []).append((item, call))
        if item.get("record_type") == "tool_result":
            all_results.setdefault(item.get("call_id") or "", []).append(item)
    if record.get("record_type") == "assistant":
        calls = record.get("calls") or []
        prior_request = None
    elif record.get("record_type") == "tool_result":
        call_id = record.get("call_id") or ""
        linked = all_calls.get(call_id) or []
        if (record.get("call_provenance") == "matched_within_frozen_range"
                and len(linked) == 1):
            calls = [linked[0][1]]
            prior_request = None
        elif record.get("call_provenance") == "matched_prior_session_record":
            prior_request = _verified_prior_call(index, record)
            if prior_request is None:
                return None, "call_provenance_unverified"
            calls = [record]
        else:
            return None, "call_provenance_unverified"
    else:
        return None, "unsupported_record_type"
    if not calls:
        return None, "missing_call"
    evidence: list[dict[str, str]] = []
    receipts = snapshot.get("process_file_receipts") or []
    for call in calls:
        call_id = call.get("call_id") or ""
        results = all_results.get(call_id) or []
        if (not call_id or (prior_request is None
                            and len(all_calls.get(call_id) or []) != 1)
                or not 1 <= len(results) <= 8):
            return None, "call_result_pair_unverified"
        result = results[0]
        result_signature = (result.get("content_sha256"), result.get("is_error"),
                            result.get("result_status"), result.get("result_code"),
                            result.get("tool"), result.get("input_fingerprint"))
        if any((item.get("content_sha256"), item.get("is_error"),
                item.get("result_status"), item.get("result_code"),
                item.get("tool"), item.get("input_fingerprint"))
               != result_signature for item in results[1:]):
            return None, "conflicting_duplicate_results"
        result_refs = sorted({f"{item['id']}:{item['sha256']}" for item in results})
        if (result.get("is_error") or str(result.get("result_status") or "").lower()
                in {"error", "failed", "cancelled", "unknown"}
                or str(result.get("result_code") or "").lower()
                not in {"", "0", "ok", "success"}):
            return None, "error_effect_unverified"
        if call.get("tool") not in _PROVABLE_FILE_WRITERS:
            return None, "tool_effect_scope_unverified"
        candidates = [item for item in receipts if isinstance(item, dict)
                      and item.get("tool_id") == call_id]
        if len(candidates) == 1 and _file_receipt_is_current(candidates[0], call, index):
            receipt = candidates[0]
            evidence.append({"call_id": call_id, "result_id": result["id"],
                             "result_sha256": result["sha256"],
                             "result_refs": result_refs,
                             "proof": "host_capture", "host_receipt_id": receipt["id"],
                             "file_sha256": receipt["sha256"]})
        elif not candidates and call.get("tool") == "WriteFile":
            current_sha = _write_intent_matches_current(
                index, (prior_request if prior_request is not None else
                        index.read(all_calls[call_id][0][0]["id"])), call, result,
            )
            if not current_sha:
                return None, "current_write_intent_unverified"
            evidence.append({"call_id": call_id, "result_id": result["id"],
                             "result_sha256": result["sha256"],
                             "result_refs": result_refs,
                             "proof": "source_and_current_bytes",
                             "file_sha256": current_sha})
        else:
            return None, "host_file_receipt_unverified"
    return {"kind": "current_host_file_effect", "calls": evidence}, "verified"


class HostReconciliationLedger:
    """Append-only host attempts; a stored `verified` is re-proved on every read."""

    def __init__(self, index: PausedHandoffIndex, snapshot: dict[str, Any], *,
                 work_dir: str | Path | None = None, read_only: bool = False) -> None:
        _source_matches(index, snapshot)
        self.index = index
        self.read_only = read_only
        self.snapshot = dict(snapshot)
        if work_dir is not None:
            from mewcode.orchestration.chat_reports import ChatReportStore

            chat = ChatReportStore().snapshot(
                work_dir=work_dir,
                conversation_id=str(snapshot.get("conversation_id") or ""),
            ) or {}
            if chat.get("conversation_id") == snapshot.get("conversation_id"):
                receipts = list(snapshot.get("process_file_receipts") or [])
                seen = {item.get("id") for item in receipts if isinstance(item, dict)}
                for item in chat.get("process_files") or []:
                    if isinstance(item, dict) and item.get("id") not in seen:
                        receipts.append(item)
                        seen.add(item.get("id"))
                self.snapshot["process_file_receipts"] = receipts
        self.path = index.manifest_path.with_suffix(".reconciliation.jsonl")
        self.records = {item["id"]: item for item in index.manifest["records"]}
        self.latest: dict[str, dict[str, Any]] = {}
        self._head = "0" * 64
        # Archived UI readback cannot create locks or new reconciliation facts.
        # An incomplete or conflicting concurrent read fails validation below.
        with nullcontext() if read_only else _journal_lock(self.path):
            if self.path.exists():
                with self.path.open("rb") as handle:
                    for raw in handle:
                        event = json.loads(raw)
                        digest = event.pop("event_sha256", None)
                        record = self.records.get(event.get("record_id"))
                        if (event.get("policy_version") != RECONCILIATION_VERSION
                                or event.get("source_id") != index.manifest["id"]
                                or record is None
                                or event.get("record_sha256") != record["sha256"]
                                or event.get("previous_sha256") != self._head
                                or digest != hashlib.sha256(_canonical(event)).hexdigest()):
                            raise ValueError("host reconciliation receipt chain is invalid")
                        event["event_sha256"] = digest
                        self.latest[record["id"]] = event
                        self._head = digest

    def verified(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for record_id, event in self.latest.items():
            if event.get("status") != "verified":
                continue
            evidence, _reason = _prove_record(self.index, self.snapshot,
                                              self.records[record_id])
            if evidence is not None and evidence == event.get("evidence"):
                result[record_id] = event["event_sha256"]
        return result

    def reconcile(self, record_id: str) -> dict[str, Any]:
        _source_matches(self.index, self.snapshot)
        return self._reconcile_checked(record_id)

    def reconcile_many(self, record_ids: list[str]) -> list[dict[str, Any]]:
        """Check one bounded page against a single verified source snapshot."""
        if len(record_ids) > 50 or len(record_ids) != len(set(record_ids)):
            raise ValueError("invalid reconciliation batch")
        _source_matches(self.index, self.snapshot)
        return [self._reconcile_checked(record_id) for record_id in record_ids]

    def _reconcile_checked(self, record_id: str) -> dict[str, Any]:
        if self.read_only:
            raise PermissionError("read-only handoff archive cannot record evidence")
        record = self.records.get(record_id)
        if record is None or not hard_review_hold(record):
            raise ValueError("record is not a current hard reconciliation item")
        evidence, reason = _prove_record(self.index, self.snapshot, record)
        status = "verified" if evidence is not None else "unresolved"
        prior = self.latest.get(record_id)
        if (prior and prior.get("status") == status and prior.get("evidence") == evidence
                and prior.get("reason") == reason):
            return prior
        event = {"policy_version": RECONCILIATION_VERSION,
                 "source_id": self.index.manifest["id"],
                 "record_id": record_id, "record_sha256": record["sha256"],
                 "status": status, "reason": reason, "evidence": evidence,
                 "at": time.time(), "previous_sha256": self._head}
        event["event_sha256"] = hashlib.sha256(_canonical(event)).hexdigest()
        with _journal_lock(self.path):
            if _journal_head(self.path) != self._head:
                raise ValueError("host reconciliation advanced concurrently; reload")
            with self.path.open("ab") as handle:
                handle.write(_canonical(event) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        self.latest[record_id] = event
        self._head = event["event_sha256"]
        return event

    def import_verified_prefix(self, previous: "HostReconciliationLedger") -> int:
        """Re-prove only an exact append-only ancestor under this generation."""
        if self.read_only:
            raise PermissionError("read-only handoff archive cannot import evidence")
        old = previous.index.manifest["source"]
        new = self.index.manifest["source"]
        if (new.get("parent_source_id") != previous.index.manifest["id"]
                or any(old.get(key) != new.get(key) for key in (
                    "run_id", "conversation_id", "contract_fingerprint",
                    "log_path", "start",
                )) or old.get("end", 0) >= new.get("end", 0)):
            raise ValueError("reconciliation prefix is not an exact source ancestor")
        previous.index.verify(original=True)
        old_records = previous.index.manifest["records"]
        new_prefix = self.index.manifest["records"][:len(old_records)]
        if [(item["id"], item["sha256"]) for item in old_records] != [
                (item["id"], item["sha256"]) for item in new_prefix]:
            raise ValueError("reconciliation source prefix changed")
        migrated = 0
        record_ids = list(previous.verified())
        for offset in range(0, len(record_ids), 50):
            for receipt in self.reconcile_many(record_ids[offset:offset + 50]):
                if receipt["status"] == "verified":
                    migrated += 1
        return migrated


def unresolved_hard_holds(index: PausedHandoffIndex,
                          verified: dict[str, str]) -> list[tuple[dict[str, Any], str]]:
    return [(record, reason) for record in index.manifest["records"]
            if (reason := hard_review_hold(record)) and record["id"] not in verified]


def reconciliation_worklist(
    index: PausedHandoffIndex, snapshot: dict[str, Any], *,
    offset: int = 0, limit: int = 25,
    work_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Page metadata and freshly re-proved receipt status, never source text."""
    if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
            or isinstance(limit, bool) or not isinstance(limit, int)
            or not 1 <= limit <= 50):
        raise ValueError("invalid reconciliation page")
    ledger = HostReconciliationLedger(index, snapshot, work_dir=work_dir, read_only=True)
    verified = ledger.verified()
    receipts_by_call = Counter(
        str(receipt.get("tool_id") or "")
        for receipt in (ledger.snapshot.get("process_file_receipts") or [])
        if isinstance(receipt, dict) and receipt.get("tool_id")
    )
    holds = [(record, hard_review_hold(record)) for record in index.manifest["records"]]
    holds = [(record, reason) for record, reason in holds if reason]
    rows = []
    for record, reason in holds[offset:offset + limit]:
        calls = _calls(record)
        tools = [_safe_tool(call.get("tool")) for call in calls]
        call_ids = {str(call.get("call_id") or "") for call in calls}
        candidates = sum(receipts_by_call[call_id] for call_id in call_ids if call_id)
        rows.append({
            "record_id": record["id"], "record_sha256": record["sha256"],
            "record_type": record.get("record_type") or "unknown",
            "reason": reason, "tools": sorted(set(tools)),
            "candidate_host_receipts": candidates,
            "required_evidence": _evidence_route(record, tools),
            "status": "verified" if record["id"] in verified else "unresolved",
            "last_attempt_reason": (ledger.latest.get(record["id"]) or {}).get("reason", ""),
        })
    return {
        "source_id": index.manifest["id"], "total": len(holds),
        "offset": offset, "limit": limit,
        "next_offset": offset + len(rows) if offset + len(rows) < len(holds) else None,
        "items": rows, "verified_count": len(verified),
        "remaining_count": len(holds) - len(verified),
        "candidate_receipts_are_not_clearance": True,
    }
