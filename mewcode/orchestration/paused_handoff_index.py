"""Private, byte-exact index for dialogue written while an expert run is paused.

The index is navigation data.  It never replaces the JSONL source and must not
be copied into the public expert-run projection.  A sealed local copy protects
the frozen range from log rotation; resuming still requires the *original*
range to match its digest and all later appended records to be reviewed.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterator


INDEX_VERSION = 3
DEFAULT_FRAGMENT_BYTES = 8_192
MAX_RECORD_METADATA_BYTES = 8_000_000
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_PATH_FIELDS = {"path", "file_path", "output_path", "input_path", "directory", "dir_path"}
READ_ONLY_TOOLS = frozenset({
    "ReadFile", "ReadAuditArtifact", "ReadStageResult", "Glob", "Grep",
    "InspectToolHistory", "AgentStatus", "BackgroundTasks", "FindSkills",
    "ProbeCapabilities", "ReadReviewQueue", "WebFetch", "WebSearch",
    "ToolSearch", "InspectGitState", "TaskList",
})
_SENSITIVE_TEXT = re.compile(
    rb"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|"
    rb"authorization|bearer\s+|\bsk-[A-Za-z0-9_-]{8,}|\b[A-Z]:[\\/]|\\\\[^\\\s]+\\|"
    rb"/(?:home|Users|private|var)/)"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_digest(manifest: dict[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    return _sha(json.dumps(body, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8"))


def _range_digest(path: Path, start: int, end: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        source.seek(start)
        remaining = end - start
        while remaining:
            block = source.read(min(65_536, remaining))
            if not block:
                raise ValueError("handoff source ended inside the frozen range")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def _fragments(data: bytes, *, absolute_start: int, record_id: str,
               limit: int) -> list[dict[str, Any]]:
    valid_utf8 = True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        valid_utf8 = False
    pieces = []
    cursor = 0
    while cursor < len(data):
        stop = min(cursor + limit, len(data))
        if valid_utf8 and stop < len(data):
            while stop > cursor:
                try:
                    data[cursor:stop].decode("utf-8")
                    break
                except UnicodeDecodeError:
                    stop -= 1
            if stop == cursor:
                raise ValueError("fragment limit cannot hold a UTF-8 code point")
        raw = data[cursor:stop]
        pieces.append({
            "id": f"{record_id}:{len(pieces)}", "record_id": record_id,
            "ordinal": len(pieces), "start": absolute_start + cursor,
            "end": absolute_start + stop, "sha256": _sha(raw),
            "utf8_valid": valid_utf8,
        })
        cursor = stop
    for piece in pieces:
        piece["total"] = len(pieces)
        piece["previous_id"] = pieces[piece["ordinal"] - 1]["id"] if piece["ordinal"] else None
        piece["next_id"] = pieces[piece["ordinal"] + 1]["id"] if piece["ordinal"] + 1 < len(pieces) else None
    return pieces


def _stream_record(reader, *, remaining: int, limit: int) -> dict[str, Any]:
    """Read one JSONL record with bounded fragment and metadata buffers."""
    digest = hashlib.sha256()
    metadata_parts: list[bytes] = []
    metadata_size = 0
    metadata_over_limit = False
    pending = bytearray()
    pieces: list[dict[str, Any]] = []
    consumed = 0
    emitted = 0
    complete_line = False
    utf8_valid = True

    def emit(count: int) -> None:
        nonlocal emitted, utf8_valid
        piece = bytes(pending[:count])
        del pending[:count]
        try:
            piece.decode("utf-8")
        except UnicodeDecodeError:
            utf8_valid = False
        pieces.append({"relative_start": emitted,
                       "relative_end": emitted + len(piece),
                       "sha256": _sha(piece)})
        emitted += len(piece)

    while consumed < remaining:
        chunk = reader.readline(min(65_536, remaining - consumed))
        if not chunk:
            raise ValueError("sealed handoff source ended early")
        consumed += len(chunk)
        digest.update(chunk)
        if not metadata_over_limit:
            if metadata_size + len(chunk) <= MAX_RECORD_METADATA_BYTES:
                metadata_parts.append(chunk)
                metadata_size += len(chunk)
            else:
                metadata_parts.clear()
                metadata_over_limit = True
        pending.extend(chunk)
        while len(pending) >= limit + 4:
            stop = limit
            while stop > 0 and pending[stop] & 0xC0 == 0x80:
                stop -= 1
            emit(stop or limit)
        if chunk.endswith(b"\n"):
            complete_line = True
            break
    while len(pending) > limit:
        stop = limit
        while stop > 0 and pending[stop] & 0xC0 == 0x80:
            stop -= 1
        emit(stop or limit)
    if pending:
        emit(len(pending))
    for piece in pieces:
        piece["utf8_valid"] = utf8_valid
    return {"length": consumed, "sha256": digest.hexdigest(),
            "complete_line": complete_line,
            "metadata_raw": None if metadata_over_limit else b"".join(metadata_parts),
            "fragments": pieces}


def _canonical_path(value: str) -> str:
    # Windows paths can appear in logs read on a non-Windows test host.
    if re.match(r"^[A-Za-z]:[\\/]", value) or "\\" in value:
        return ntpath.normcase(ntpath.normpath(value))
    return os.path.normcase(os.path.normpath(value))


def _path_values(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _PATH_FIELDS and isinstance(item, str) and item.strip():
                yield item
            elif isinstance(item, (dict, list)):
                yield from _path_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _path_values(item)


def _call_may_mutate(tool: str, arguments: dict[str, Any]) -> bool:
    """Only a proven read-only call can be treated as effect-free.

    ReviewWorkQueue refreshes its durable evidence state before even serving
    read/summary, so its apparent read actions are deliberately stateful.
    Unknown tools and parameter modes fail closed.
    """
    if tool == "BackgroundTasks":
        return bool(arguments.get("acknowledge", False))
    return tool not in READ_ONLY_TOOLS


def _record_metadata(raw: bytes, path_ids: dict[str, str], path_map: dict[str, dict]) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("record is not an object")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {"record_type": "invalid", "parse_error": "invalid_jsonl", "calls": [],
                "path_ids": [], "has_text": False, "content_sha256": _sha(raw),
                "semantic_input_safe": False}
    calls = []
    content = value.get("content")
    if value.get("type") == "assistant" and isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                call_paths = []
                for original in _path_values(arguments):
                    canonical = _canonical_path(original)
                    path_id = path_ids.get(canonical)
                    if path_id is None:
                        path_id = "path-" + _sha(canonical.encode("utf-8"))[:20]
                        path_ids[canonical] = path_id
                        path_map[path_id] = {"original": original, "canonical": canonical}
                    if path_id not in call_paths:
                        call_paths.append(path_id)
                calls.append({
                    "call_id": str(block.get("id") or ""),
                    "tool": str(block.get("name") or ""),
                    "action": str(arguments.get("action") or ""),
                    "tool_version": str(block.get("version") or ""),
                    "parameter_pattern": sorted(
                        f"{key}:{type(item).__name__}" for key, item in arguments.items()
                    ),
                    "input_fingerprint": _sha(json.dumps(
                        arguments, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), default=str,
                    ).encode("utf-8")),
                    "input_revision": str(arguments.get("input_revision") or arguments.get("record_revision") or ""),
                    "path_ids": call_paths,
                    "may_mutate": _call_may_mutate(str(block.get("name") or ""), arguments),
                })
    has_text = bool(content.strip()) if isinstance(content, str) else any(
        isinstance(block, dict) and block.get("type") == "text"
        and bool(str(block.get("text") or "").strip())
        for block in content
    ) if isinstance(content, list) else False
    resource_ids = []
    for original in _path_values(content):
        canonical = _canonical_path(original)
        path_id = path_ids.get(canonical)
        if path_id is None:
            path_id = "path-" + _sha(canonical.encode("utf-8"))[:20]
            path_ids[canonical] = path_id
            path_map[path_id] = {"original": original, "canonical": canonical}
        if path_id not in resource_ids:
            resource_ids.append(path_id)
    return {
        "record_type": str(value.get("type") or ""),
        "timestamp": str(value.get("timestamp") or ""),
        "call_id": str(value.get("tool_use_id") or ""),
        "is_error": bool(value.get("is_error")),
        "result_status": str(value.get("status") or ""),
        "result_code": str(value.get("error_code") or value.get("exit_code") or ""),
        "work_cursor": str(value.get("work_cursor") or value.get("item_id") or ""),
        "receipt_ids": [str(item) for item in (value.get("receipt_ids") or [])
                        if isinstance(item, (str, int))][:64]
                       if isinstance(value.get("receipt_ids"), list) else [],
        "calls": calls, "path_ids": resource_ids, "has_text": has_text,
        "content_sha256": _sha(json.dumps(content, ensure_ascii=False,
                                             sort_keys=True, default=str).encode("utf-8")),
        "input_revision": str(value.get("input_revision") or ""),
        "parse_error": "",
        "semantic_input_safe": not bool(_SENSITIVE_TEXT.search(raw)),
    }


def _prior_call_refs(source: Path, start: int, wanted_ids: set[str],
                     path_ids: dict[str, str], path_map: dict[str, dict]) -> dict[str, list[dict]]:
    """Resolve only exact referenced call IDs in this same session log."""
    matches: dict[str, list[dict]] = {identity: [] for identity in wanted_ids}
    if not wanted_ids or start <= 0 or start > 128 * 1024 * 1024:
        return matches
    with source.open("rb") as reader:
        while reader.tell() < start:
            record_start = reader.tell()
            streamed = _stream_record(reader, remaining=start - record_start,
                                      limit=DEFAULT_FRAGMENT_BYTES)
            record_end = reader.tell()
            if not streamed["complete_line"]:
                break
            raw = streamed["metadata_raw"]
            if raw is None:
                # An oversized prior call cannot be linked by an inferred ID.
                # The result remains an explicit unresolved provenance gap.
                continue
            local_path_ids: dict[str, str] = {}
            local_path_map: dict[str, dict] = {}
            metadata = _record_metadata(raw, local_path_ids, local_path_map)
            for call in metadata["calls"]:
                if call["call_id"] in matches:
                    for path_id in call["path_ids"]:
                        path_value = local_path_map[path_id]
                        path_ids[path_value["canonical"]] = path_id
                        path_map[path_id] = path_value
                    matches[call["call_id"]].append({
                        "call": call,
                        "source_start": record_start,
                        "source_end": record_end,
                        "source_sha256": streamed["sha256"],
                    })
    return matches


class PausedHandoffIndex:
    """One frozen generation of a single run/conversation source range."""

    def __init__(self, manifest: dict[str, Any], manifest_path: Path) -> None:
        if (manifest.get("schema_version") != INDEX_VERSION
                or manifest.get("manifest_sha256") != _manifest_digest(manifest)):
            raise ValueError("handoff index manifest integrity is invalid")
        self.manifest = manifest
        self.manifest_path = manifest_path
        self.sealed_path = manifest_path.with_suffix(".source")
        self._references: dict[str, dict[str, Any]] = {}
        for record in manifest.get("records") or []:
            for ref in (record, *(record.get("fragments") or [])):
                ref_id = ref.get("id")
                if not isinstance(ref_id, str) or ref_id in self._references:
                    raise ValueError("handoff index contains duplicate or invalid reference id")
                self._references[ref_id] = ref

    @classmethod
    def build(cls, *, log_path: str | Path, root: str | Path, run_id: str,
              conversation_id: str, contract_fingerprint: str,
              start: int, end: int | None = None,
              fragment_bytes: int = DEFAULT_FRAGMENT_BYTES,
              frozen_boundaries: dict[str, Any] | None = None,
              generation: int = 1, parent_source_id: str = "") -> "PausedHandoffIndex":
        if not _SAFE_RUN_ID.fullmatch(run_id) or not conversation_id or not contract_fingerprint:
            raise ValueError("invalid handoff source identity")
        if fragment_bytes < 4:
            raise ValueError("fragment limit must hold a UTF-8 code point")
        if not isinstance(generation, int) or generation < 1:
            raise ValueError("invalid handoff generation")
        if parent_source_id and not re.fullmatch(r"[a-f0-9]{64}", parent_source_id):
            raise ValueError("invalid prior handoff source id")
        source = Path(log_path).resolve(strict=True)
        stat = source.stat()
        end = stat.st_size if end is None else end
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start or end > stat.st_size:
            raise ValueError("invalid frozen handoff range")
        directory = Path(root) / run_id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / ("." + uuid.uuid4().hex + ".source.tmp")
        digest = hashlib.sha256()
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                reader.seek(start)
                remaining = end - start
                while remaining:
                    block = reader.read(min(65_536, remaining))
                    if not block:
                        raise ValueError("source changed during handoff sealing")
                    writer.write(block)
                    digest.update(block)
                    remaining -= len(block)
                writer.flush()
                os.fsync(writer.fileno())
            span_sha = digest.hexdigest()
            identity = _sha(json.dumps({
                "version": INDEX_VERSION, "run_id": run_id,
                "conversation_id": conversation_id,
                "contract_fingerprint": contract_fingerprint,
                "source": str(source), "start": start, "end": end,
                "sha256": span_sha, "fragment_bytes": fragment_bytes,
                "frozen_boundaries": frozen_boundaries or {},
                "generation": generation, "parent_source_id": parent_source_id,
            }, sort_keys=True).encode("utf-8"))
            manifest_path = directory / f"{identity}.index.json"
            sealed_path = manifest_path.with_suffix(".source")
            if not sealed_path.exists():
                os.replace(temporary, sealed_path)
            else:
                temporary.unlink()
                if _range_digest(sealed_path, 0, end - start) != span_sha:
                    raise ValueError("existing sealed handoff source differs")
            if manifest_path.exists():
                return cls.load(manifest_path)
            records: list[dict[str, Any]] = []
            path_ids: dict[str, str] = {}
            path_map: dict[str, dict] = {}
            with sealed_path.open("rb") as reader:
                relative = 0
                while relative < end - start:
                    record_start = start + relative
                    streamed = _stream_record(reader, remaining=end - start - relative,
                                              limit=fragment_bytes)
                    relative += streamed["length"]
                    # Record references must survive an append-only next
                    # generation so verified prefix receipts can be reused.
                    record_id = "record-" + _sha(
                        f"{run_id}:{conversation_id}:{contract_fingerprint}:"
                        f"{source}:{record_start}:{start + relative}:{streamed['sha256']}".encode()
                    )[:24]
                    if streamed["metadata_raw"] is None:
                        metadata = {"record_type": "invalid", "parse_error": "record_metadata_too_large",
                                    "calls": [], "path_ids": [], "has_text": False,
                                    "content_sha256": streamed["sha256"],
                                    "semantic_input_safe": False}
                    else:
                        metadata = _record_metadata(streamed["metadata_raw"],
                                                    path_ids, path_map)
                    fragments = []
                    for ordinal, piece in enumerate(streamed["fragments"]):
                        fragments.append({
                            "id": f"{record_id}:{ordinal}", "record_id": record_id,
                            "ordinal": ordinal,
                            "start": record_start + piece["relative_start"],
                            "end": record_start + piece["relative_end"],
                            "sha256": piece["sha256"],
                            "utf8_valid": piece["utf8_valid"],
                        })
                    for ordinal, piece in enumerate(fragments):
                        piece["total"] = len(fragments)
                        piece["previous_id"] = fragments[ordinal - 1]["id"] if ordinal else None
                        piece["next_id"] = fragments[ordinal + 1]["id"] if ordinal + 1 < len(fragments) else None
                    records.append({
                        "id": record_id, "start": record_start,
                        "end": start + relative, "sha256": streamed["sha256"],
                        "complete_line": streamed["complete_line"],
                        **metadata,
                        "fragments": fragments,
                    })
            within_matches: dict[str, list[dict]] = {}
            for record in records:
                for call in record["calls"]:
                    if call["call_id"]:
                        within_matches.setdefault(call["call_id"], []).append(call)
            calls_by_id = {call_id: calls[0] for call_id, calls in within_matches.items()
                           if len(calls) == 1}
            unmatched = {record["call_id"] for record in records
                         if record["record_type"] == "tool_result"
                         and record["call_id"] not in within_matches}
            prior_matches = _prior_call_refs(source, start, unmatched, path_ids, path_map)
            prior_call_refs = []
            for call_id, matches in prior_matches.items():
                if len(matches) == 1:
                    match = matches[0]
                    calls_by_id[call_id] = match["call"]
                    prior_call_refs.append({
                        "call_id": call_id,
                        "start": match["source_start"],
                        "end": match["source_end"],
                        "sha256": match["source_sha256"],
                    })
            for record in records:
                if record["record_type"] == "tool_result" and record["call_id"] in calls_by_id:
                    call = calls_by_id[record["call_id"]]
                    record["tool"] = call["tool"]
                    record["action"] = call["action"]
                    record["path_ids"] = call["path_ids"]
                    for key in ("tool_version", "parameter_pattern", "input_fingerprint", "input_revision", "may_mutate"):
                        record[key] = call[key]
                    record["call_provenance"] = (
                        "matched_prior_session_record" if record["call_id"] in prior_matches
                        and len(prior_matches[record["call_id"]]) == 1
                        else "matched_within_frozen_range"
                    )
                elif record["record_type"] == "tool_result":
                    record["call_provenance"] = (
                        "ambiguous_within_frozen_range"
                        if len(within_matches.get(record["call_id"], [])) > 1
                        else "ambiguous_prior" if len(prior_matches.get(record["call_id"], [])) > 1
                        else "unmatched"
                    )
            manifest = {
                "schema_version": INDEX_VERSION, "id": identity,
                "source": {"run_id": run_id, "conversation_id": conversation_id,
                           "contract_fingerprint": contract_fingerprint,
                           "log_format_version": "session-jsonl-v1",
                           "generation": generation,
                           "parent_source_id": parent_source_id,
                           "log_path": str(source), "file_identity": [stat.st_dev, stat.st_ino],
                           "start": start, "end": end, "sha256": span_sha},
                "records": records, "path_map": path_map,
                "prior_call_refs": prior_call_refs,
                "frozen_boundaries": frozen_boundaries or {},
                "unmatched_tool_results": sum(
                    record.get("call_provenance") in {
                        "unmatched", "ambiguous_prior", "ambiguous_within_frozen_range",
                    }
                    for record in records
                ),
            }
            manifest["manifest_sha256"] = _manifest_digest(manifest)
            payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
            staged = manifest_path.with_suffix(".index.tmp")
            with staged.open("x", encoding="utf-8") as writer:
                writer.write(payload)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(staged, manifest_path)
            return cls(manifest, manifest_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def load(cls, path: str | Path) -> "PausedHandoffIndex":
        manifest_path = Path(path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != INDEX_VERSION:
            raise ValueError("unsupported handoff index version")
        return cls(manifest, manifest_path)

    def read(self, reference_id: str) -> bytes:
        source_start = self.manifest["source"]["start"]
        ref = self._references.get(reference_id)
        if ref is None:
            raise KeyError(reference_id)
        with self.sealed_path.open("rb") as reader:
            reader.seek(ref["start"] - source_start)
            raw = reader.read(ref["end"] - ref["start"])
        if _sha(raw) != ref["sha256"]:
            raise ValueError("handoff source fragment digest mismatch")
        return raw

    def find_records(self, *, offset: int | None = None,
                     call_id: str = "", path_id: str = "",
                     input_revision: str = "", result_code: str = "",
                     limit: int = 100) -> list[dict[str, Any]]:
        """Return bounded navigation metadata, never chat or tool content."""
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("invalid handoff index query limit")
        if offset is not None and (not isinstance(offset, int) or offset < 0):
            raise ValueError("invalid handoff source offset")
        found = []
        for record in self.manifest["records"]:
            if offset is not None and not record["start"] <= offset < record["end"]:
                continue
            if call_id and call_id != record.get("call_id") and not any(
                call.get("call_id") == call_id for call in record.get("calls") or []
            ):
                continue
            if path_id and path_id not in (record.get("path_ids") or []):
                continue
            if input_revision and input_revision != record.get("input_revision") and not any(
                call.get("input_revision") == input_revision for call in record.get("calls") or []
            ):
                continue
            if result_code and result_code != record.get("result_code"):
                continue
            found.append({"record_id": record["id"], "start": record["start"],
                          "end": record["end"], "sha256": record["sha256"],
                          "record_type": record["record_type"],
                          "fragment_ids": [fragment["id"] for fragment in record["fragments"]]})
            if len(found) >= limit:
                break
        return found

    def verify(self, *, original: bool = True) -> dict[str, Any]:
        if self.manifest.get("manifest_sha256") != _manifest_digest(self.manifest):
            raise ValueError("handoff index manifest integrity changed")
        source = self.manifest["source"]
        cursor = source["start"]
        for record in self.manifest["records"]:
            if record["start"] != cursor:
                raise ValueError("handoff record coverage gap or overlap")
            part_cursor = cursor
            for fragment in record["fragments"]:
                if fragment["start"] != part_cursor:
                    raise ValueError("handoff fragment coverage gap or overlap")
                self.read(fragment["id"])
                part_cursor = fragment["end"]
            if part_cursor != record["end"]:
                raise ValueError("handoff record is not fully fragmented")
            cursor = record["end"]
        if cursor != source["end"]:
            raise ValueError("handoff range is not fully indexed")
        if self.sealed_path.stat().st_size != cursor - source["start"]:
            raise ValueError("sealed handoff source length mismatch")
        if _range_digest(self.sealed_path, 0, cursor - source["start"]) != source["sha256"]:
            raise ValueError("sealed handoff source digest mismatch")
        if original:
            path = Path(source["log_path"])
            try:
                stat = path.stat()
                if stat.st_size < source["end"] or [stat.st_dev, stat.st_ino] != source["file_identity"]:
                    raise ValueError("original handoff source changed")
                if _range_digest(path, source["start"], source["end"]) != source["sha256"]:
                    raise ValueError("original handoff source digest mismatch")
                for ref in self.manifest.get("prior_call_refs") or []:
                    if (_range_digest(path, ref["start"], ref["end"]) != ref["sha256"]
                            or ref["end"] > source["start"]):
                        raise ValueError("prior handoff call provenance changed")
            except OSError as exc:
                raise ValueError("original handoff source unavailable") from exc
        return {"records": len(self.manifest["records"]),
                "fragments": sum(len(row["fragments"]) for row in self.manifest["records"]),
                "bytes": cursor - source["start"], "complete": True}
