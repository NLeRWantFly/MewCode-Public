"""Read-only source profiling for expert run snapshots; emits timings only."""
from __future__ import annotations

import argparse
import copy
import json
import tempfile
import time
from pathlib import Path

from mewcode.expert_runs import ExpertRunStore, _compact_snapshot_events, _normalize_snapshot


def timed(fn):
    started = time.perf_counter()
    value = fn()
    return value, round((time.perf_counter() - started) * 1000, 2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    if not args.snapshot.is_file():
        parser.error("snapshot file does not exist")
    raw, read_ms = timed(args.snapshot.read_bytes)
    snapshot, parse_ms = timed(lambda: json.loads(raw))
    if not isinstance(snapshot, dict):
        parser.error("snapshot is not an object")
    copied, deepcopy_ms = timed(lambda: copy.deepcopy(snapshot))
    _, normalize_ms = timed(lambda: _normalize_snapshot(copied))
    _, compact_ms = timed(lambda: _compact_snapshot_events(copied))
    encoded, serialize_ms = timed(lambda: json.dumps(copied, ensure_ascii=False, indent=2))
    with tempfile.TemporaryDirectory(prefix="mewcode-run-profile-") as temp:
        store = ExpertRunStore(temp)
        _, persist_ms = timed(lambda: store._persist(copied))
        _, repeat_persist_ms = timed(lambda: store._persist(copied))
        run_id = str(copied.get("run_id") or "")
        disk_snapshot_bytes = (Path(temp) / f"{run_id}.json").stat().st_size
        blobs = list((Path(temp) / "blobs" / run_id).glob("*.json"))
        blob_bytes = sum(path.stat().st_size for path in blobs)
        store._live[run_id] = copied
        _, event_append_ms = timed(lambda: store.append_sequenced_events(run_id, [{
            "type": "expert_step", "seq": int(copied.get("last_seq") or 0) + 1,
            "label": "profile", "status": "running"}], return_snapshot=False))
    print(json.dumps({"schema_version": 1, "source_bytes": len(raw),
        "serialized_bytes": len(encoded.encode("utf-8")),
        "read_ms": read_ms, "parse_ms": parse_ms, "deepcopy_ms": deepcopy_ms,
        "disk_snapshot_bytes": disk_snapshot_bytes, "blob_count": len(blobs),
        "blob_bytes": blob_bytes,
        "normalize_ms": normalize_ms, "compact_ms": compact_ms,
        "serialize_ms": serialize_ms, "persist_and_public_projection_ms": persist_ms,
        "repeat_persist_and_projection_ms": repeat_persist_ms,
        "event_append_and_return_ms": event_append_ms},
        sort_keys=True))


if __name__ == "__main__":
    main()
