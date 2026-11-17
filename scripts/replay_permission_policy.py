"""Build and inspect a redacted permission-policy replay fixture.

The historical audit log stores a 200-character parameter preview, not a
replayable tool call. This utility therefore keeps two evidence streams
separate: documented pre-P0 counters and actual counters reconstructed from a
sanitized audit snapshot. It never replays truncated history as a successful
decision. Synthetic adversarial inputs are evaluated through the current
PermissionChecker without executing any command.
"""
from __future__ import annotations

import ast
import argparse
import hashlib
import json
import ntpath
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_DIR = ROOT / "tests" / "fixtures" / "permission-replay"
PINNED_BASELINE = {
    "policy_id": "pre-p0-documented-2026-09-24",
    "model_reviews": 351,
    "model_review_allows": 333,
    "model_review_denies": 18,
    "workspace_external_read_asks": 43,
    "human_decisions": 40,
    "isolation_guardrail_denies": 1,
}

_SENSITIVE = re.compile(
    r"(?i)(?:\.ssh[\\/]|\.aws[\\/]|\.azure[\\/]|"
    r"\.config[\\/]gcloud[\\/]|providers\.json|usage-accounts\.json|"
    r"\.git-credentials|\.netrc|id_rsa|id_ed25519|(?:^|[\\/])\.env(?:$|[.\\/])|"
    r"\.(?:pem|key|pfx)(?:$|[?\s])|(?:api[_-]?key|password|secret|bearer|token))"
)
_VALUE_KEYS = frozenset({"command", "file_path", "path", "target", "script", "url", "cwd"})
_TOOL_CATEGORIES = {
    "Bash": "command", "RunVerification": "command",
    "TerminalStart": "command", "TerminalInput": "command",
    "ReadFile": "read", "Grep": "read", "Glob": "read",
    "WriteFile": "write", "EditFile": "write", "ReportArtifact": "write",
    "Agent": "orchestration", "TaskStop": "orchestration",
    "TaskRetry": "orchestration", "human_oncall": "orchestration",
    "BackgroundTasks": "orchestration", "AgentStatus": "orchestration",
    "TaskList": "orchestration", "ToolSearch": "orchestration",
    "LoadSkill": "orchestration", "InspectGitState": "orchestration",
    "DecideNextStep": "orchestration", "ProbeCapabilities": "orchestration",
    "report_step": "orchestration",
}
_COMMAND_ALIASES = frozenset({
    "python", "py", "pytest", "ruff", "mypy", "git", "powershell", "pwsh",
    "cmd", "curl", "wget", "invoke-webrequest", "remove-item", "get-content",
    "get-childitem", "get-item", "select-string", "get-filehash", "test-path",
    "rg", "grep", "cat", "type", "pip", "npm", "node", "cargo", "docker",
    "bash", "sh", "sqlite3", "pandoc", "ruff.exe", "python.exe", "py.exe",
})
_DECISIONS = frozenset({"allow", "deny", "ask", "user_allow"})
_SOURCE_LAYER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _iter_interesting_values(value: Any, key: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            if str(child_key).lower() in _VALUE_KEYS and isinstance(child_value, str):
                yield child_value
            else:
                yield from _iter_interesting_values(child_value, str(child_key))
    elif isinstance(value, list):
        for child in value:
            yield from _iter_interesting_values(child, key)
    elif isinstance(value, str) and key.lower() in _VALUE_KEYS:
        yield value


def _extract_payload(params_summary: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(params_summary)
    except (TypeError, json.JSONDecodeError):
        # Historical logger versions used Python's repr(dict) rather than
        # JSON. literal_eval accepts only Python literals; never use eval here.
        try:
            payload = ast.literal_eval(params_summary)
        except (SyntaxError, ValueError, TypeError):
            return None
    return payload if isinstance(payload, dict) else None


def _is_under_windows_root(path_text: str, root_text: str) -> bool:
    if not re.match(r"^[A-Za-z]:[\\/]", path_text) or not re.match(r"^[A-Za-z]:[\\/]", root_text):
        return False
    try:
        candidate = ntpath.normcase(ntpath.abspath(path_text))
        root = ntpath.normcase(ntpath.abspath(root_text)).rstrip("\\/")
        return candidate == root or candidate.startswith(root + "\\")
    except (OSError, ValueError):
        return False


def _path_class(values: Iterable[str], *, workspace_root: str) -> str:
    items = list(values)
    if not items:
        return "none"
    joined = "\n".join(items)
    if _SENSITIVE.search(joined):
        return "sensitive"
    for item in items:
        if _is_under_windows_root(item.strip().strip("\"'"), workspace_root):
            return "workspace_internal"
        try:
            candidate = Path(item.strip().strip("\"'"))
            root = Path(workspace_root)
            if candidate.is_absolute() and root.is_absolute() and candidate.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
                return "workspace_internal"
        except (OSError, RuntimeError, ValueError, AttributeError):
            pass
    return "external_regular"


def _command_head(command: str) -> str:
    normalized = " ".join(command.replace("\\", "/").strip().split())
    if re.fullmatch(
        r"(?i)(?:[a-z]:)?(?:[^ ]*/)?(?:python(?:\.exe)?)\s+"
        r"(?:[^ ]*/)?tools/stage5_prepare_evidence\.py\s+verify-progress",
        normalized,
    ):
        return "python_stage5_verify_progress"
    first = re.match(r"^(?:\"([^\"]+)\"|'([^']+)'|(\S+))", command.strip())
    if not first:
        return "unknown"
    executable = next((part for part in first.groups() if part is not None), "")
    basename = ntpath.basename(executable).lower()
    if basename in {"python.exe", "python"}:
        return "python"
    if basename in {"py.exe", "py"}:
        return "py"
    if basename not in _COMMAND_ALIASES:
        return "other"
    return basename.removesuffix(".exe")


def sanitize_audit_line(
    line: str,
    *,
    ordinal: int,
    workspace_root: str,
) -> dict[str, Any]:
    """Map one raw audit line to an allowlisted, non-identifying record."""
    try:
        raw = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    tool = raw.get("tool_name")
    tool_name = tool if isinstance(tool, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", tool) else "unknown"
    category = _TOOL_CATEGORIES.get(tool_name, "other")
    decision = raw.get("decision")
    decision = decision if decision in _DECISIONS else "unknown"
    layer = raw.get("source_layer")
    source_layer = layer if isinstance(layer, str) and _SOURCE_LAYER_RE.fullmatch(layer) else "unknown"
    params = raw.get("params_summary")
    summary = params if isinstance(params, str) else ""
    truncated = len(summary) >= 200 or summary.endswith("...")
    payload = None if truncated else _extract_payload(summary)
    if payload is None:
        return {
            "id": f"audit-{ordinal:06d}", "tool": tool_name, "category": category,
            "command_head": "unknown", "path_class": "unknown", "decision": decision,
            "source_layer": source_layer, "truncated": bool(truncated), "quality": "unknown",
        }

    values = list(_iter_interesting_values(payload))
    command = payload.get("command")
    if isinstance(command, str):
        head = _command_head(command)
        values.append(command)
    else:
        head = "none"
    path_class = _path_class(values, workspace_root=workspace_root)
    return {
        "id": f"audit-{ordinal:06d}", "tool": tool_name, "category": category,
        "command_head": head, "path_class": path_class, "decision": decision,
        "source_layer": source_layer, "truncated": False, "quality": "complete",
    }


def _source_metrics(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "model_reviews": sum(row.get("source_layer") == "auto_safety_reviewer" for row in records),
        "model_review_allows": sum(row.get("source_layer") == "auto_safety_reviewer" and row.get("decision") == "allow" for row in records),
        "model_review_denies": sum(row.get("source_layer") == "auto_safety_reviewer" and row.get("decision") == "deny" for row in records),
        "workspace_external_read_asks": sum(
            row.get("category") == "read" and row.get("decision") == "ask"
            and row.get("path_class") == "external_regular" and row.get("quality") == "complete"
            for row in records
        ),
        "human_decisions": sum(row.get("source_layer") == "human_decision" for row in records),
        "isolation_guardrail_denies": sum(
            row.get("source_layer") == "isolation_guardrail" and row.get("decision") == "deny"
            for row in records
        ),
    }


def build_fixture(source: Path, output_dir: Path, *, workspace_root: str) -> dict[str, Any]:
    raw_bytes = source.read_bytes()
    lines = raw_bytes.decode("utf-8-sig", errors="replace").splitlines()
    nonblank_lines = [line for line in lines if line.strip()]
    records = [sanitize_audit_line(line, ordinal=index + 1, workspace_root=workspace_root)
               for index, line in enumerate(nonblank_lines)]
    decisions_bytes = b"".join(_canonical_json(row) + b"\n" for row in records)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "decisions.jsonl").write_bytes(decisions_bytes)

    observed = _source_metrics(records)
    unknown = sum(row["quality"] == "unknown" for row in records)
    manifest = {
        "schema_version": 1,
        "purpose": "redacted permission-policy replay input; never an authorization source",
        "provenance": {
            "source_name": source.name,
            "source_path_redacted": True,
            "source_sha256": _sha256(raw_bytes),
            "source_line_count": len(nonblank_lines),
            "physical_line_count": len(lines),
            "sanitized_record_count": len(records),
            "documented_source_line_count": 489,
            "line_count_delta_from_documented": len(lines) - 489,
            "source_count_discrepancy": len(lines) != 489,
            "raw_parameters_persisted": False,
        },
        "policy_baseline": dict(PINNED_BASELINE),
        "source_observed_metrics": observed,
        "coverage": {
            "complete_records": len(records) - unknown,
            "unknown_records": unknown,
            "unknown_records_are_successes": False,
            "source_line_count_is_complete": len(nonblank_lines) == len(records),
        },
        "decisions_sha256": _sha256(decisions_bytes),
    }
    adversarial_path = output_dir / "adversarial.jsonl"
    if adversarial_path.exists():
        manifest["adversarial_sha256"] = _sha256(adversarial_path.read_bytes())
    (output_dir / "manifest.json").write_bytes(_canonical_json(manifest) + b"\n")
    return manifest


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at {path.name}:{line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"invalid row at {path.name}:{line_number}")
        rows.append(value)
    return rows


def verify_fixture(fixture_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    manifest_path = fixture_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("permission replay manifest is missing or invalid") from exc
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported permission replay schema")
    decisions_path = fixture_dir / "decisions.jsonl"
    adversarial_path = fixture_dir / "adversarial.jsonl"
    decisions_bytes = decisions_path.read_bytes()
    if _sha256(decisions_bytes) != manifest.get("decisions_sha256"):
        raise ValueError("permission replay decisions digest mismatch")
    records = load_jsonl(decisions_path)
    adversarial = load_jsonl(adversarial_path)
    if len(records) != manifest.get("provenance", {}).get("sanitized_record_count"):
        raise ValueError("permission replay record count mismatch")
    allowed_record_keys = {
        "id", "tool", "category", "command_head", "path_class", "decision",
        "source_layer", "truncated", "quality",
    }
    for row in records:
        if set(row) != allowed_record_keys:
            raise ValueError("permission replay record contains an unexpected field")
        if row.get("quality") == "unknown" and (
            row.get("command_head") != "unknown" or row.get("path_class") != "unknown"
        ):
            raise ValueError("unknown audit rows cannot be classified as a pass")
    expected_hash = manifest.get("adversarial_sha256")
    actual_hash = _sha256(adversarial_path.read_bytes())
    if expected_hash is not None and expected_hash != actual_hash:
        raise ValueError("permission replay adversarial digest mismatch")
    return manifest, records, adversarial


@dataclass(frozen=True)
class _ReplayTool:
    name: str
    category: str
    interaction_kind: str = ""


def _evaluate_adversarial(cases: list[dict[str, Any]], *, project_root: Path) -> dict[str, Any]:
    """Run only permission checks for synthetic inputs; never execute tools."""
    from mewcode.permissions.checker import PermissionChecker
    from mewcode.permissions.dangerous import DangerousCommandDetector
    from mewcode.permissions.modes import PermissionMode
    from mewcode.permissions.rules import RuleEngine
    from mewcode.permissions.sandbox import PathSandbox

    counts: Counter[str] = Counter()
    results: list[dict[str, str]] = []
    junction_status = "not_applicable"
    (project_root / ".tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="permission-policy-replay-", dir=project_root / ".tmp") as temp_name:
        temp_root = Path(temp_name)
        for case in cases:
            case_id = str(case.get("id", "unknown"))
            case_root = temp_root / case_id
            case_root.mkdir()
            sandbox = PathSandbox(str(case_root))
            # Isolate synthetic paths from the default temp-root allowance so
            # external placeholders stay external to the policy under test.
            sandbox._allowed_roots = [case_root.resolve()]
            checker = PermissionChecker(DangerousCommandDetector(), sandbox, RuleEngine(), PermissionMode.AUTO)
            name = str(case.get("tool", "Bash"))
            category = str(case.get("category", "command"))
            arguments = case.get("arguments")
            if not isinstance(arguments, dict):
                raise ValueError(f"adversarial case {case_id} has no structured input")
            if case_id == "adv-032":
                reports = case_root / "reports"
                reports.mkdir()
                outside = temp_root / "outside-target"
                outside.mkdir()
                link = reports / "junction"
                try:
                    link.symlink_to(outside, target_is_directory=True)
                except (NotImplementedError, OSError, PermissionError):
                    junction_status = "unknown"
                    counts["unknown"] += 1
                    results.append({"id": case_id, "effect": "unknown"})
                    continue
                junction_status = "verified_symlink"
                arguments = {"file_path": str(link / "outside.txt")}
            decision = checker.check(_ReplayTool(name, category), arguments)
            effect = str(decision.effect)
            counts[effect] += 1
            results.append({"id": case_id, "effect": effect})
    return {
        "total": len(cases), "allow": counts.get("allow", 0),
        "deny": counts.get("deny", 0), "ask": counts.get("ask", 0),
        "review": counts.get("review", 0), "unknown": counts.get("unknown", 0),
        "allowed_case_ids": [row["id"] for row in results if row["effect"] == "allow"],
        "unknown_case_ids": [row["id"] for row in results if row["effect"] == "unknown"],
        "junction_escape": junction_status,
        "policy_id": "worktree-permission-checker-v1",
    }


def report(fixture_dir: Path, *, assert_integrity: bool = False) -> dict[str, Any]:
    manifest, records, adversarial = verify_fixture(fixture_dir)
    actual = _source_metrics(records)
    unknown_count = sum(row.get("quality") == "unknown" for row in records)
    baseline_differences = {
        key: {"documented": PINNED_BASELINE[key], "observed": actual[key]}
        for key in PINNED_BASELINE if key != "policy_id" and actual[key] != PINNED_BASELINE[key]
    }
    # The low-level checker is safe to run against fake names/commands because
    # this path calls check() only and never dispatches a tool.
    adversarial_result = _evaluate_adversarial(adversarial, project_root=ROOT)
    report_value = {
        "policy_versions": {
            "baseline": PINNED_BASELINE["policy_id"],
            "candidate": adversarial_result["policy_id"],
        },
        "documented_baseline": dict(PINNED_BASELINE),
        "source_observed_metrics": actual,
        "baseline_mismatch": {
            "detected": bool(baseline_differences),
            "kind": "source_snapshot_drift_not_policy_regression",
            "differences": baseline_differences,
            "documented_rows": manifest.get("provenance", {}).get("documented_source_line_count"),
            "observed_rows": manifest.get("provenance", {}).get("source_line_count"),
        },
        "coverage": {
            "records": len(records), "complete": len(records) - unknown_count,
            "unknown": unknown_count, "unknown_counted_as_success": 0,
        },
        "historical_replay_limitation": (
            "Audit parameters are truncated previews; only complete allowlisted metadata is comparable. "
            "A source counter mismatch or unknown record cannot certify the P0 metric gate."
        ),
        "adversarial_candidate": adversarial_result,
        "assertions": {
            "fixture_integrity": True,
            "adversarial_allow_count_zero": adversarial_result["allow"] == 0,
            "adversarial_unknown_count_zero": adversarial_result["unknown"] == 0,
            "baseline_source_reconciled": not baseline_differences,
            "no_unknown_records": unknown_count == 0,
        },
    }
    if assert_integrity:
        failed = [name for name, passed in report_value["assertions"].items() if not passed]
        if failed:
            raise AssertionError("permission replay gate failed: " + ", ".join(failed))
    return report_value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-dir", type=Path, default=DEFAULT_FIXTURE_DIR)
    parser.add_argument("--build-from-source", type=Path,
                        help="read-only import of an audit JSONL; writes only the sanitized fixture")
    parser.add_argument("--workspace-root", default=str(ROOT),
                        help="workspace root used only to reduce paths to a category")
    parser.add_argument("--assert", dest="assert_integrity", action="store_true",
                        help="fail on integrity, baseline drift, unknown rows, or adversarial allows")
    args = parser.parse_args(argv)
    try:
        if args.build_from_source is not None:
            manifest = build_fixture(args.build_from_source, args.fixture_dir, workspace_root=args.workspace_root)
            print(json.dumps({"built": True, "records": manifest["provenance"]["sanitized_record_count"],
                              "unknown": manifest["coverage"]["unknown_records"],
                              "source_count_discrepancy": manifest["provenance"]["source_count_discrepancy"]},
                             sort_keys=True))
            return 0
        result = report(args.fixture_dir, assert_integrity=args.assert_integrity)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (AssertionError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
