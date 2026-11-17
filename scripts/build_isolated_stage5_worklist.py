"""Build a source-bound, *unapproved* stage-5 revalidation worklist.

The archived predecessor checkpoint and immutable source bytes are verified
before any old stage-5 result is admitted as a hint. This command never edits
the original workspace or expert run, and it never promotes a prior PASS.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mewcode.expert_checkpoints import ExpertCheckpointArchive


PREDECESSORS = (
    "n_stage0_env_probe", "n_stage1_cloud_channel",
    "n_stage2_sample_dual_read_cost", "n_stage3_full_cloud_parse",
    "n_stage4_gemini_dual_read",
)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def relative_file(root: Path, value: str) -> Path:
    normalized = value.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (not value or relative.is_absolute() or ":" in normalized
            or any(part in {"", ".", ".."} for part in relative.parts)):
        raise ValueError(f"unsafe relative source path: {value!r}")
    path = root.joinpath(*relative.parts).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"source path escapes the isolated workspace: {value!r}")
    return path


def page_key(item: dict) -> tuple[str, int]:
    return str(item["doc_id"]), int(item["pdf_page"])


def unique_pages(items: list[dict], label: str) -> dict[tuple[str, int], dict]:
    result = {page_key(item): item for item in items}
    if len(result) != len(items):
        raise ValueError(f"duplicate page key in {label}")
    return result


def build(isolation: Path, snapshot_path: Path, *, run_id: str,
          checkpoint_id: str, scope_sha256: str) -> dict:
    isolation = isolation.resolve(strict=True)
    baseline = (isolation / "baseline").resolve(strict=True)
    work = (isolation / "work").resolve(strict=True)
    if not baseline.is_relative_to(isolation) or not work.is_relative_to(isolation):
        raise ValueError("isolated paths are not contained in the requested root")
    meta, anchor = ExpertCheckpointArchive(
        isolation / "checkpoint" / "turn-checkpoints"
    ).load(run_id, checkpoint_id)
    if (meta["status"] != "completed" or meta["completed_nodes"] != 5
            or any((anchor.get("node_states") or {}).get(node, {}).get("status") != "completed"
                   for node in PREDECESSORS)
            or any("stage5" in node for node in anchor.get("node_states") or {})):
        raise ValueError("the isolated checkpoint is not the completed stage-4 anchor")

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if snapshot.get("run_id") != run_id or snapshot.get("status") != "paused":
        raise ValueError("current source run is not the expected paused run")
    old_nodes = {item["node_id"]: item for item in anchor["locked_flow"]["nodes"]}
    new_nodes = {item["node_id"]: item for item in snapshot["locked_flow"]["nodes"]}
    if any(old_nodes.get(node) != new_nodes.get(node) for node in PREDECESSORS):
        raise ValueError("a completed predecessor changed in the current contract")

    scope_path = relative_file(work, "reports/page_diff_v2/suspicious_pages.json")
    if digest(scope_path) != scope_sha256:
        raise ValueError("frozen 241-page scope changed")
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    pages = unique_pages(
        [item for item in scope["pages"] if item.get("tier") in {"T2", "T3"}],
        "frozen scope",
    )
    if len(pages) != 241:
        raise ValueError("frozen scope is not exactly 241 distinct pages")
    queue = snapshot["review_queues"]["n_stage5_diff_audit"]
    if queue.get("manifest_sha256") != scope_sha256:
        raise ValueError("host queue does not bind the frozen scope")
    host_items = unique_pages(
        [{**item["identity"], "item_id": item["item_id"], "image_path": item["image_path"]}
         for item in queue["items"]], "host queue",
    )
    with relative_file(work, "reports/stage5_page_dispositions.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as stream:
        dispositions = unique_pages(list(csv.DictReader(stream)), "observed dispositions")
    if set(host_items) != set(pages) or set(dispositions) != set(pages):
        raise ValueError("host queue, observed dispositions, and frozen scope differ")

    fingerprint_path = isolation / "source_fingerprints.json"
    fingerprints = json.loads(fingerprint_path.read_text(encoding="utf-8"))
    if fingerprints.get("frozen_scope_sha256") != scope_sha256:
        raise ValueError("source fingerprint set does not bind the frozen scope")
    source_sha: dict[str, str] = {}
    for item in fingerprints["entries"]:
        path_key = item["path"].replace("\\", "/")
        expected = item["sha256"].lower()
        for root in (baseline, work):
            path = relative_file(root, item["path"])
            if path.stat().st_size != item["bytes"] or digest(path) != expected:
                raise ValueError(f"isolated source fingerprint differs: {path_key}")
        if path_key in source_sha:
            raise ValueError(f"duplicate source fingerprint: {path_key}")
        source_sha[path_key] = expected

    issues = json.loads(relative_file(work, "reports/stage5_issues.json").read_text(encoding="utf-8"))
    candidate_hints = unique_pages(issues.get("items") or [], "prior candidates")
    if not set(candidate_hints) <= set(pages):
        raise ValueError("prior candidate is outside the frozen scope")
    rows = []
    prior_states = Counter()
    for key in sorted(pages):
        host = host_items[key]
        disposition = dispositions[key]
        image = host["image_path"].replace("\\", "/")
        if image != disposition["image_path"].replace("\\", "/"):
            raise ValueError(f"image identity differs: {key}")
        item_id = host["item_id"]
        state = (queue.get("records") or {}).get(item_id, {}).get("state", "unrecorded")
        prior_states[state] += 1
        sources = {}
        for name, path_value in (
            ("image", image), ("mineru", disposition["mineru_path"]),
            ("gemini", disposition["gemini_path"]),
        ):
            path_key = path_value.replace("\\", "/")
            if path_key not in source_sha:
                raise ValueError(f"missing immutable {name} fingerprint: {key}")
            sources[name] = {"path": path_key, "sha256": source_sha[path_key]}
        candidate = candidate_hints.get(key) or {}
        rows.append({
            "doc_id": key[0], "pdf_page": key[1], "tier": pages[key]["tier"],
            "original_item_id": item_id, "source": sources,
            "prior_host_state_hint": state,
            "prior_candidate_hint": {
                "status": candidate.get("status", ""),
                "evidence_path": candidate.get("evidence_path", ""),
            },
            "revalidation_state": "pending_independent_visual_review",
        })
    return {
        "schema_version": 1,
        "kind": "isolated_stage5_revalidation_worklist",
        "source": {
            "run_id": run_id,
            "checkpoint_id": checkpoint_id,
            "checkpoint_sha256": meta["sha256"],
            "archived_flow_version": anchor["flow_version"],
            "current_flow_version": snapshot["flow_version"],
            "current_flow_fingerprint": snapshot["flow_fingerprint"],
            "current_snapshot_sha256": digest(snapshot_path),
            "frozen_scope_sha256": scope_sha256,
            "fingerprint_manifest_sha256": digest(fingerprint_path),
            "host_queue_revision_hint": queue["revision"],
        },
        "policy": {
            "preserved_completed_predecessors": list(PREDECESSORS),
            "prior_stage5_states_are_untrusted_hints": True,
            "acceptance_and_user_signoff_unchanged": True,
            "original_run_and_workspace_untouched": True,
        },
        "summary": {
            "total": len(rows), "pending_independent_visual_review": len(rows),
            "independently_accepted": 0,
            "prior_host_states_are_hints": dict(sorted(prior_states.items())),
            "prior_candidates_are_hints": len(candidate_hints),
            "verified_frozen_source_files": len(source_sha),
        },
        "items": rows,
    }


def verify_existing_worklist(existing: dict, current: dict) -> str:
    """Accept only irrelevant snapshot-byte drift, never worklist drift.

    The full snapshot SHA is retained as provenance, but it also changes for
    unrelated run metadata.  ``build`` has already rechecked the checkpoint,
    frozen scope, all source fingerprints, queue identities and every item.
    This compatibility check does not modify the old worklist or admit any
    stage-5 result as approved.
    """
    if existing == current:
        return "exact"
    if not isinstance(existing, dict) or not isinstance(current, dict):
        raise ValueError("existing isolated worklist differs from verified source")
    old_source = existing.get("source")
    new_source = current.get("source")
    if not isinstance(old_source, dict) or not isinstance(new_source, dict):
        raise ValueError("existing isolated worklist differs from verified source")
    old_hash = old_source.get("current_snapshot_sha256")
    new_hash = new_source.get("current_snapshot_sha256")
    if not all(isinstance(value, str) and len(value) == 64
               and all(char in "0123456789abcdef" for char in value)
               for value in (old_hash, new_hash)):
        raise ValueError("existing isolated worklist has invalid snapshot provenance")
    old_without_hash = {**existing, "source": {
        key: value for key, value in old_source.items()
        if key != "current_snapshot_sha256"}}
    new_without_hash = {**current, "source": {
        key: value for key, value in new_source.items()
        if key != "current_snapshot_sha256"}}
    if old_without_hash != new_without_hash:
        raise ValueError("existing isolated worklist differs from verified source")
    return "snapshot_metadata_drift_only"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--isolation-root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--checkpoint-id", required=True)
    parser.add_argument("--scope-sha256", required=True)
    parser.add_argument("--verify-existing", action="store_true")
    args = parser.parse_args()
    result = build(
        args.isolation_root, args.snapshot, run_id=args.run_id,
        checkpoint_id=args.checkpoint_id, scope_sha256=args.scope_sha256,
    )
    output = args.isolation_root / "stage5_revalidation_worklist.json"
    verification = "created"
    if args.verify_existing:
        verification = verify_existing_worklist(
            json.loads(output.read_text(encoding="utf-8")), result)
    else:
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps({"output": str(output), "summary": result["summary"],
                      "verification": verification}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
