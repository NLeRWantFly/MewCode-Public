"""Read-only, fail-closed interpretation of a synthetic joint-trial export."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

KINDS = ("nul", "cwd", "git_version", "git_status", "powershell")
NAMES = tuple(prefix + kind for prefix in ("ac_", "lpac_") for kind in KINDS)
CAPABILITIES = ["registryRead", "lpacInstrumentation", "lpacCom"]


def summarize(inspection: dict, manifest_bytes: bytes) -> dict:
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema") != "mewcode-native-host-acl-trial-v1":
        raise ValueError("unexpected manifest schema")
    trial = inspection.get("host-acl-trial-result.json", {})
    if trial.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest():
        raise ValueError("manifest hash mismatch")
    cases = inspection.get("trial_case_receipts", {})
    if set(cases) != set(NAMES):
        raise ValueError("missing or unexpected case receipts")
    requests = manifest.get("cases", [])
    if len(requests) != len(NAMES) or {case.get("name") for case in requests} != set(NAMES):
        raise ValueError("manifest case identity mismatch")
    objects = inspection.get("objects", [])
    restored = (trial.get("restore_verified") is True
                and len(objects) == 2
                and {obj.get("label") for obj in objects} == {"nul", "drive_root"}
                and all(obj.get("original_dacl_sha256")
                        and obj["original_dacl_sha256"] == obj.get("current_dacl_sha256")
                        and type(obj.get("original_controls")) is int
                        and obj["original_controls"] == obj.get("current_controls")
                        and obj.get("original_sddl")
                        and obj["original_sddl"] == obj.get("current_sddl") for obj in objects))
    rows = []
    for name in NAMES:
        receipt = cases[name]
        lpac = name.startswith("lpac_")
        kind = name.removeprefix("lpac_" if lpac else "ac_")
        clean = all(receipt.get(key) is True for key in
                    ("job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok"))
        identity = (receipt.get("appcontainer_verified") is True
                    and receipt.get("lpac_verified") is True
                    and receipt.get("capabilities") == (CAPABILITIES if lpac else [])
                    and receipt.get("lpac_verification_method") in {"access_check", "token_query"})
        if receipt.get("lpac_verification_method") == "access_check":
            identity = identity and type(receipt.get("lpac_access_mask")) is int and receipt["lpac_access_mask"] == (2 if lpac else 3)
        intact = (all(receipt.get(key) is False for key in
                      ("timed_out", "cancelled", "stdout_truncated", "stderr_truncated"))
                  and receipt.get("error") == "" and type(receipt.get("exit_code")) is int
                  and isinstance(receipt.get("stdout"), str))
        lines = receipt.get("stdout", "").splitlines() if isinstance(receipt.get("stdout"), str) else []
        if kind == "nul":
            markers = ["nul-read:open=ok", "nul-write:open=ok", "nul-readwrite:open=ok"]
        elif kind == "cwd":
            markers = ["git_mingw_getcwd=ok", "drive_root_attributes=ok", "drive_root_metadata_open=ok"]
        elif kind == "powershell":
            markers = ["MEWCODE_POWERSHELL_HOST_CONTROL_OK"]
        else:
            markers = []
        observed = all(lines.count(marker) == 1 for marker in markers)
        if kind == "git_version":
            observed = len(lines) == 1 and lines[0].startswith("git version ")
        passed = identity and clean and intact and receipt.get("exit_code") == 0 and observed
        rows.append({"case": name, "requested_mode": "lpac" if lpac else "appcontainer",
                     "exit_code": receipt.get("exit_code"), "identity_verified": bool(identity),
                     "cleanup_confirmed": clean, "evidence_intact": intact, "passed": bool(passed)})
    infrastructure = (restored and trial.get("watchdog_ready") is True
                      and trial.get("watchdog_exited") is True
                      and type(trial.get("watchdog_exit_code")) is int and trial["watchdog_exit_code"] == 0
                      and not trial.get("error") and not trial.get("restoration_error")
                      and all(row["identity_verified"] and row["cleanup_confirmed"] and row["evidence_intact"] for row in rows))
    return {"schema": "mewcode-native-host-acl-summary-v1",
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "case_count": len(rows), "passed_case_count": sum(row["passed"] for row in rows),
            "restore_verified": bool(restored), "infrastructure_complete": bool(infrastructure),
            "compatibility_passed": bool(infrastructure and all(row["passed"] for row in rows)),
            "production_authorized": False, "duration_ms": trial.get("duration_ms"), "cases": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--inspection", type=Path, required=True)
    parser.add_argument("--assert-compatible", action="store_true")
    args = parser.parse_args()
    result = summarize(json.loads(args.inspection.read_text(encoding="utf-8-sig")), args.manifest.read_bytes())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.assert_compatible and not result["compatibility_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
