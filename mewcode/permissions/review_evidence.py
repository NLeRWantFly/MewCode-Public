"""Bounded review views; observations are evidence, never authorization.

Select latest task observations BEFORE reducing their size. An unavailable
latest observation must not silently expose an older, more permissive state.
Original tool results remain in conversation/session storage unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass

from mewcode.permissions.decision_envelope import canonical_digest, redact_value
from mewcode.permissions import visual_review

MAX_SOURCE_CHARS = 256_000
MAX_ROW_BYTES = 2400
MAX_INVENTORY_ROW_BYTES = 6400
MAX_TOTAL_BYTES = 8000
MAX_ROWS = 6
INVENTORY_COLUMNS = ("id", "name", "status", "dispatch_relation", "cancellation_pending",
                     "cleanup_confirmed", "cleanup_failed", "native_phase",
                     "native_process_started", "native_cleanup_confirmed")
TASK_FIELDS = (
    "id", "name", "status", "parent_task_id", "owner_task_id", "owner_session_id",
    "dispatch_turn_id", "current_turn_id", "dispatch_relation", "created_at",
    "cancellation_pending", "cleanup_confirmed", "cleanup_pending", "cleanup_failed", "cleanup_error",
    "elapsed", "duration_basis", "duration_quality", "native_execution",
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(text):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    def invalid(_):
        raise ValueError("non-finite JSON")

    if not isinstance(text, str) or len(text) > MAX_SOURCE_CHARS:
        return None
    try:
        return json.loads(text, object_pairs_hook=unique, parse_constant=invalid)
    except (ValueError, RecursionError):
        return None


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 128


def _row(result, tool, value=None, *, reason="", task_id=None):
    text = result.content or ""
    row = {
        "tool_use_id": result.tool_use_id, "tool": tool,
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source_chars": len(text), "trusted_authorization": False,
        "complete": not reason, "omission_reason": reason,
        "text": (redact_value(text) if value is None else _json(redact_value(value))),
    }
    if task_id:
        row["task_id"] = task_id
    return row


def _unavailable(result, tool, reason, task_id=None, extra=None):
    payload = {"observation": "unavailable", "reason": reason,
        "task_id": task_id, "older_state_is_not_current": True}
    if extra:
        payload.update(extra)
    return _row(result, tool, payload, reason=reason, task_id=task_id)


def _invalid_native(item):
    native = item.get("native_execution")
    return native is not None and (not isinstance(native, dict) or any(
        key in native and native[key] is not None and type(native[key]) is not bool
        for key in ("process_started", "cleanup_confirmed")))


def _needs_task_detail(item):
    if (item.get("status") == "running" or item.get("cancellation_pending") is True
            or item.get("dispatch_relation") == "current_turn"
            or item.get("cleanup_confirmed") is not True
            or item.get("cleanup_failed") is True or _invalid_native(item)):
        return True
    native = item.get("native_execution")
    return bool(native) and native.get("phase") not in {"finished", "not_started"}


def _task_row(result, tool, item):
    # Preserve the whole native state: do not cherry-pick a success field while
    # omitting a cancellation/cleanup/error field. Oversize => unavailable.
    if _invalid_native(item):
        return _unavailable(result, tool, "invalid_native_state", item["id"])
    projection = {key: item[key] for key in TASK_FIELDS if key in item}
    projection["observation_kind"] = "task_state_at_query_time_not_live_probe"
    row = _row(result, tool, projection,
        reason="task_state_projection; task_instruction_and_result_not_included",
        task_id=item["id"])
    if len(_json(row).encode("utf-8")) > MAX_ROW_BYTES:
        return _unavailable(result, tool, "latest_task_state_exceeds_view_limit", item["id"])
    return row


def evidence_context(conversation):
    if conversation is None:
        return []
    # Only matched host tool-call records identify structured status results.
    # JSON in arbitrary output or assistant prose is not a task snapshot.
    calls, ambiguous = {}, set()
    for message in conversation.history:
        for call in getattr(message, "tool_uses", ()):
            if call.tool_use_id in calls:
                ambiguous.add(call.tool_use_id)
            calls[call.tool_use_id] = call

    seen_tasks, seen_generic = set(), set()
    full_task_snapshot_seen = False
    agent_status_config_seen = False
    # Candidates carry a sortable (message, result, sequence) position so the
    # selected view stays chronological, inventory before its own detail rows.
    task_candidates, generic_candidates = [], []

    for mi in range(len(conversation.history) - 1, -1, -1):
        message = conversation.history[mi]
        for ri in range(len(message.tool_results) - 1, -1, -1):
            result = message.tool_results[ri]
            call = calls.get(result.tool_use_id) if result.tool_use_id not in ambiguous else None
            tool = call.tool_name if call else "unbound_tool_result"
            args = call.arguments if call and isinstance(call.arguments, dict) else {}
            if tool in {"BackgroundTasks", "AgentStatus"}:
                task_id = args.get("task_id") if tool == "BackgroundTasks" else None
                # Truncated model-facing output is unparseable; the host keeps a
                # structured observation that is never sent to providers.
                observed = getattr(result, "host_observation", None)
                if isinstance(observed, dict) and "tool_observation" in observed:
                    observed = observed["tool_observation"]
                if task_id is not None:
                    if not _identity(task_id):
                        row = _unavailable(result, tool, "invalid_task_query_identity")
                        row["observation_source"] = "tool_output_text"
                        task_candidates.append(((mi, ri, 0), row, ("task", None)))
                        continue
                    if full_task_snapshot_seen or task_id in seen_tasks:
                        continue
                    seen_tasks.add(task_id)
                    item = None if result.is_error else (
                        observed if isinstance(observed, dict) else _decode(result.content))
                    source = ("host_structured_result"
                              if not result.is_error and isinstance(observed, dict)
                              else "tool_output_text")
                    if not isinstance(item, dict) or item.get("id") != task_id:
                        row = _unavailable(result, tool, "latest_task_query_failed_or_invalid", task_id)
                    else:
                        row = _task_row(result, tool, item)
                    row["observation_source"] = source
                    task_candidates.append(((mi, ri, 0), row, ("task", task_id)))
                    continue

                if full_task_snapshot_seen:
                    # The newest inventory supersedes task lists, but the latest
                    # AgentStatus still carries configuration/current_turn facts.
                    if tool == "AgentStatus" and not agent_status_config_seen:
                        agent_status_config_seen = True
                        data = None if result.is_error else (
                            observed if isinstance(observed, dict)
                            else _decode(result.content))
                        source = ("host_structured_result"
                                  if not result.is_error and isinstance(observed, dict)
                                  else "tool_output_text")
                        if not isinstance(data, dict):
                            row = _unavailable(result, tool,
                                "latest_agent_status_failed_or_invalid")
                        else:
                            row = _row(result, tool,
                                {"observation_kind": "agent_status_at_query_time",
                                 "tasks_superseded_by_newer_inventory": True,
                                 **{key: data[key] for key in
                                    ("host_work_dir", "ui_endpoint", "permission_mode", "runtime_route",
                                     "current_turn") if key in data}},
                                reason="status_projection; task_list_superseded_by_newer_inventory")
                            if len(_json(row).encode("utf-8")) > MAX_ROW_BYTES:
                                row = _unavailable(result, tool,
                                    "agent_status_exceeds_view_limit")
                        row["observation_source"] = source
                        task_candidates.append(((mi, ri, 0), row, ("agent_status", None)))
                    continue
                full_task_snapshot_seen = True
                if tool == "AgentStatus":
                    # This AgentStatus becomes the latest inventory; its row
                    # already carries the configuration keys.
                    agent_status_config_seen = True
                data = None if result.is_error else (
                    observed if isinstance(observed, (dict, list)) else _decode(result.content))
                source = ("host_structured_result"
                          if not result.is_error and isinstance(observed, (dict, list))
                          else "tool_output_text")
                items = data.get("tasks") if isinstance(data, dict) and tool == "AgentStatus" else data
                if (not isinstance(items, list) or
                        any(not isinstance(item, dict) or not _identity(item.get("id")) for item in items) or
                        len({item["id"] for item in items}) != len(items)):
                    row = _unavailable(result, tool, "latest_task_inventory_failed_or_invalid")
                    row["observation_source"] = source
                    task_candidates.append(((mi, ri, 0), row, ("inventory", False)))
                    continue
                # Preserve inventory coverage, including an empty snapshot, and
                # current configuration without including old prompts/results.
                superseded = [item["id"] for item in items if item["id"] in seen_tasks]
                seq = 0
                if any(_invalid_native(item) for item in items):
                    row = _unavailable(result, tool, "invalid_native_state")
                    row["observation_source"] = source
                    task_candidates.append(((mi, ri, seq), row, ("inventory", False)))
                    seq += 1
                else:
                    entries = []
                    for item in items:
                        if item["id"] in seen_tasks:
                            entries.append([item.get("id"), item.get("name")] + [None] * 8)
                            continue
                        native = item.get("native_execution")
                        native = native if isinstance(native, dict) else {}
                        entries.append([item.get("id"), item.get("name"), item.get("status"),
                            item.get("dispatch_relation"), item.get("cancellation_pending"),
                            item.get("cleanup_confirmed"), item.get("cleanup_failed"),
                            native.get("phase"), native.get("process_started"),
                            native.get("cleanup_confirmed")])
                    current_turn_id = next((item.get("current_turn_id") for item in items
                                            if item.get("current_turn_id") is not None), None)
                    if current_turn_id is None and tool == "AgentStatus" and isinstance(data, dict):
                        turn = data.get("current_turn")
                        if isinstance(turn, dict):
                            current_turn_id = turn.get("turn_id")
                    inventory = {"observation_kind": "task_inventory_at_query_time",
                        "inventory_complete": True,
                        "coverage": "every task owned by this conversation at query time",
                        "task_count": len(items),
                        "current_turn_id": current_turn_id,
                        "current_turn_task_ids": [item["id"] for item in items
                            if item.get("dispatch_relation") == "current_turn"],
                        "superseded_by_newer_observation_ids": superseded,
                        "columns": list(INVENTORY_COLUMNS),
                        "tasks": entries,
                        "older_task_snapshots_superseded": True}
                    if tool == "AgentStatus" and isinstance(data, dict):
                        inventory.update({key: data[key] for key in
                            ("host_work_dir", "ui_endpoint", "permission_mode", "runtime_route", "current_turn")
                            if key in data})
                    row = _row(result, tool, inventory,
                        reason="status_projection; non_state_fields_not_included")
                    complete = len(_json(row).encode("utf-8")) <= MAX_INVENTORY_ROW_BYTES
                    if not complete:
                        row = _unavailable(result, tool, "task_inventory_exceeds_view_limit",
                            extra={"task_count": len(items)})
                    row["observation_source"] = source
                    task_candidates.append(((mi, ri, seq), row, ("inventory", complete)))
                    seq += 1
                # A long history of completed tasks must not crowd out the
                # active/current-turn task whose native startup is being checked.
                ordered = sorted(reversed(items), key=lambda item: (
                    item.get("status") != "running" and item.get("cancellation_pending") is not True,
                    item.get("dispatch_relation") != "current_turn"))
                for item in ordered:
                    if item["id"] in seen_tasks or not _needs_task_detail(item):
                        continue
                    row = _task_row(result, tool, item)
                    row["observation_source"] = source
                    task_candidates.append(((mi, ri, seq), row, ("task", item["id"])))
                    seq += 1
                for item in items:
                    seen_tasks.add(item["id"])
                continue

            if result.is_error or not result.content:
                continue  # failed reviews are not new evidence for retrying them
            safe = redact_value(result.content)
            if safe in seen_generic:
                continue
            seen_generic.add(safe)
            row = _row(result, tool)
            if len(safe) > 2000 or len(_json(row).encode("utf-8")) > MAX_ROW_BYTES:
                row = _unavailable(result, tool, "tool_output_exceeds_view_limit")
            generic_candidates.append(((mi, ri, 0), row, ("generic", None)))

    # Task-state evidence outranks generic observations under the same budget.
    selected, used = [], 0
    omitted_task_state_ids, omitted_task_rows = [], 0
    omitted_generic_observation_count = 0
    omitted_task_inventory = inventory_complete_selected = False
    omitted_agent_status = False

    for position, row, (kind, tag) in [*task_candidates, *generic_candidates]:
        cost = len(_json(row).encode("utf-8"))
        # Reserve space for an explicit coverage notice. Never fall back to
        # older same-task data just because a latest row does not fit.
        if len(selected) >= MAX_ROWS or used + cost > MAX_TOTAL_BYTES - 800:
            if kind == "inventory":
                omitted_task_inventory = True
            elif kind == "task":
                omitted_task_rows += 1
                if tag is not None:
                    omitted_task_state_ids.append(tag)
            elif kind == "agent_status":
                omitted_agent_status = True
            else:
                omitted_generic_observation_count += 1
            continue
        selected.append((position, row))
        used += cost
        if kind == "inventory" and tag:
            inventory_complete_selected = True

    rows = [row for _, row in sorted(selected, key=lambda entry: entry[0])]
    omitted = (omitted_task_rows + int(omitted_task_inventory)
               + int(omitted_agent_status) + omitted_generic_observation_count)
    if omitted:
        rows.append({"text": _json({"reason": "bounded_evidence_selection",
            "omitted_observation_count": omitted,
            "omitted_task_state_ids": omitted_task_state_ids,
            "omitted_task_inventory": omitted_task_inventory,
            "omitted_agent_status": omitted_agent_status,
            "omitted_generic_observation_count": omitted_generic_observation_count,
            "absence_is_not_evidence": not inventory_complete_selected}),
            "complete": False, "omission_reason": "bounded_evidence_selection",
            "trusted_authorization": False})
    return rows


def evidence_cache_material(rows):
    """A new tool-call ID alone is not new evidence; content/coverage changes are."""
    return sorted({_json({k: v for k, v in row.items() if k != "tool_use_id"}) for row in rows})


def evidence_progress_material(rows):
    """Compare host state for retry progress; keep approval cache stricter.

    Tool discovery and omitted task prose can change the raw output hash while
    its host-projected state is identical. That alone cannot reopen a denial.
    """
    projected = {"status_projection; non_state_fields_not_included",
                 "status_projection; task_list_superseded_by_newer_inventory",
                 "task_state_projection; task_instruction_and_result_not_included"}
    return evidence_cache_material([
        {key: value for key, value in row.items() if key not in {"source_sha256", "source_chars"}}
        if row.get("observation_source") == "host_structured_result"
            and row.get("omission_reason") in projected else row
        for row in rows])


# The existing typed reviewer admits state + a question at 30,000 UTF-8 bytes
# (all questions: 60,000). 7,500 characters is the corresponding conservative
# four-byte bound for evidence packets. This is not a universal request limit:
# the selected client's admission hook owns its action/policy capacity check.
MAX_TEXT_CHARS = 7_500
MAX_TEXT_BYTES = 30_000
REVIEW_EVIDENCE_UNAVAILABLE = (
    '本次审查需要查看图像，但审核模型不支持图片且未设置看图能力；'
    '请在设置的“审核”中选择看图能力模型。操作未执行，也未降级为文本审查。'
)


class ReviewEvidenceInputError(visual_review.VisualReviewInputError):
    """Local, unpaid admission failure; old visual-input handlers remain valid."""

    def __init__(self, reason, *, sizes, failure_kind='evidence_input'):
        super().__init__(reason, sizes=sizes)
        self.failure_kind = failure_kind


def preflight_review_text(client, payload, system):
    """Use the selected provider's limits without changing the complete request.

    Jev exposes an exact UTF-8 admission check. Ordinary model APIs enforce
    their own context windows; a Jev packet budget must not reject them before
    they can review the unchanged authorization and action.
    """
    preflight = getattr(client, 'preflight_safety_review', None)
    if callable(preflight):
        preflight(payload, system)


class ReviewEvidenceUnavailable(ValueError):
    code = 'review_evidence_unavailable'
    blocker_kind = 'authority'

    def __init__(self, reason=REVIEW_EVIDENCE_UNAVAILABLE):
        super().__init__(reason)


@dataclass(frozen=True)
class ReviewEvidence:
    text_blocks: tuple[dict, ...] = ()
    images: tuple[dict, ...] = ()
    required_channels: frozenset[str] = frozenset({'text'})


@dataclass(frozen=True)
class EvidencePlan:
    packets: tuple[dict, ...]
    manifest: tuple[dict, ...]
    digest: str
    sizes: dict


def supports_vision(client, route=None):
    """Require an explicit capability fact, never infer vision from model names."""
    if getattr(client, 'review_only', False) is True:
        return False
    if (route or {}).get('supports_vision') is True:
        return True
    capabilities = getattr(client, 'capabilities', None)
    if getattr(capabilities, 'supports_vision', False) is True:
        return True
    if isinstance(capabilities, (tuple, list, set, frozenset)) and 'vision' in capabilities:
        return True
    declared = (route or {}).get('capabilities')
    return isinstance(declared, (tuple, list, set, frozenset)) and 'vision' in declared


def evidence_from_payload(payload, images=(), *, requires_pixel=False):
    blocks = payload.get('untrusted_tool_evidence') or ()
    channels = {'text'}
    if images or requires_pixel:
        channels.add('pixel')
    if any(isinstance(block, dict) and block.get('channel') == 'command' for block in blocks):
        channels.add('command')
    return ReviewEvidence(tuple(copy.deepcopy(blocks)), tuple(copy.deepcopy(images)), frozenset(channels))


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def plan_review_evidence(evidence: ReviewEvidence, *, text_char_limit=MAX_TEXT_CHARS) -> EvidencePlan:
    """Plan every source before calling a model; no resizing, pruning, or truncation."""
    if not isinstance(evidence, ReviewEvidence):
        raise TypeError('evidence must be ReviewEvidence')
    limit = max(1, min(MAX_TEXT_CHARS, int(text_char_limit)))
    sizes = {'text_chars_limit_per_packet': limit, 'text_bytes_limit_per_packet': MAX_TEXT_BYTES,
             'image_limit_per_request': visual_review.MAX_IMAGES,
             'image_bytes_limit_per_request': visual_review.MAX_IMAGE_BYTES,
             'batch_limit': visual_review.MAX_BATCHES, 'plan_version': visual_review.PLAN_VERSION}
    if not evidence.required_channels.issubset({'text', 'pixel', 'command'}):
        raise ReviewEvidenceInputError('审查证据通道无效；操作未执行。', sizes=sizes)
    if 'pixel' in evidence.required_channels and not evidence.images:
        raise ReviewEvidenceInputError('本次审查需要实际图像，但未提供像素证据；操作未执行。',
                                       sizes=sizes, failure_kind='visual_input')
    try:
        visual = visual_review.plan_visual_review(evidence.images)
    except visual_review.VisualReviewInputError as exc:
        raise ReviewEvidenceInputError(str(exc), sizes={**sizes, **exc.sizes},
                                       failure_kind='visual_input') from exc

    texts, manifests, current, current_chars, current_bytes = [], [], [], 0, 2
    total_chars = 0
    for block_index, original in enumerate(evidence.text_blocks):
        if not isinstance(original, dict):
            raise ReviewEvidenceInputError('文本证据必须包含可追溯的对象记录。',
                                           sizes=sizes, failure_kind='text_input')
        block = copy.deepcopy(original)
        text = block.get('text')
        if not isinstance(text, str):
            # Structured observations remain lossless JSON evidence.
            text = json.dumps(block, ensure_ascii=False, sort_keys=True)
            block = {'text': text, 'source': 'structured_evidence', 'channel': block.get('channel', 'text')}
        channel = 'command' if block.get('channel') == 'command' else 'text'
        digest = canonical_digest(original)
        offset = 0
        while offset < len(text) or offset == 0:
            part = text[offset:offset + limit]
            fragment = {**block, 'text': part, 'source_digest': digest,
                        'source_index': block_index, 'char_offset': offset, 'source_chars': len(text)}
            while _bytes(fragment) > MAX_TEXT_BYTES and part:
                part = part[:max(0, len(part) // 2)]
                fragment['text'] = part
            size = _bytes(fragment)
            if size > MAX_TEXT_BYTES or not part and text:
                raise ReviewEvidenceInputError('文本证据来源标签超过请求容量；未截断原始证据。',
                    sizes={**sizes, 'oversize_source_index': block_index, 'source_fragment_bytes': size},
                    failure_kind='text_input')
            if current and (current_chars + len(part) > limit or current_bytes + size + 2 > MAX_TEXT_BYTES):
                texts.append(tuple(current))
                current, current_chars, current_bytes = [], 0, 2
            evidence_id = channel + ':' + canonical_digest([digest, block_index, offset, part])
            fragment['evidence_id'] = evidence_id
            current.append(fragment)
            current_chars += len(part)
            current_bytes += size + 2
            total_chars += len(part)
            manifests.append({'evidence_id': evidence_id, 'channel': channel,
                              'sources': copy.deepcopy(block.get('_review_sources') or [block.get('source', '')]),
                              'bytes': size, 'packet': len(texts) + 1, 'source_index': block_index,
                              'source_digest': digest, 'char_offset': offset, 'chars': len(part)})
            if not text:
                break
            offset += len(part)
    if current:
        texts.append(tuple(current))
    count = max(len(texts), len(visual.batches), 1)
    sizes.update(text_chars=total_chars, text_block_count=len(evidence.text_blocks),
                 image_count=len(evidence.images), image_bytes=visual.sizes['image_bytes'], batch_count=count)
    if count > visual_review.MAX_BATCHES:
        failure_kind = ('evidence_input' if evidence.images and evidence.text_blocks else
                        'visual_input' if evidence.images else 'text_input')
        raise ReviewEvidenceInputError('证据累计超过单次审查的分包预算；原证据保留，操作未执行。',
                                       sizes=sizes, failure_kind=failure_kind)
    for row in visual.manifest:
        manifests.append({**copy.deepcopy(row), 'evidence_id': 'pixel:' + row['image_id'],
                          'channel': 'pixel', 'bytes': row['serialized_bytes'], 'packet': row['batch']})
    packets = tuple({'text': texts[index] if index < len(texts) else (),
                     'image_indexes': visual.batches[index] if index < len(visual.batches) else ()}
                    for index in range(count))
    manifest = tuple(manifests)
    digest = canonical_digest({'version': visual_review.PLAN_VERSION, 'manifest': manifest, 'packets': packets})
    return EvidencePlan(packets, manifest, digest, sizes)


def packet_payload(payload, plan, packet_index):
    """The original authorization, exact action, and their source bindings stay fixed."""
    packet = plan.packets[packet_index]
    scoped = copy.deepcopy(payload)
    scoped['untrusted_tool_evidence'] = list(copy.deepcopy(packet['text']))
    metadata = {'plan_digest': plan.digest, 'manifest': list(copy.deepcopy(plan.manifest)),
                'packet': packet_index + 1, 'packet_count': len(plan.packets),
                'attached_indices': list(packet['image_indexes']),
                'text_evidence_ids': [block['evidence_id'] for block in packet['text']],
                'trusted_authorization': False}
    scoped['evidence_packet'] = metadata
    # Historical visual packet consumers remain readable during the transition.
    if any(row['channel'] == 'pixel' for row in plan.manifest):
        scoped['visual_evidence'] = metadata
    return scoped


def review_route(route, *, channels, pixel_access, **extra):
    return {**{key: value for key, value in (route or {}).items() if key != 'kind'},
            'channels': sorted(channels), 'pixel_access': pixel_access, **extra}
