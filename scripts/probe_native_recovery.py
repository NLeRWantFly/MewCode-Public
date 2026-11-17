"""Live synthetic recovery handoff; host fault injection, real native helper.

The user must confirm normal broker UAC. All state stays in new project .tmp
fixtures and an explicitly isolated data root. This never enables production.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.native_acceptance_executor import AcceptanceExecutor, bounded_directory
from scripts.probe_lpac_production import build_checker
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.sandbox import lpac_runtime as lpac, staged_execution as execution
from mewcode.sandbox import staged_publication as publication
from mewcode.sandbox.cleanup_evidence import read_cleanup_evidence
from mewcode.sandbox.metadata_broker import close_session
from mewcode.sandbox.native_gate import installed_bundle_identity, machine_guid
from mewcode.sandbox.recovery_handoff import preview
from mewcode.sandbox.transaction_registry import TransactionRegistry, workspace_write_block


async def run(args):
    data = bounded_directory(args.data_root)
    if Path(os.environ.get("MEWCODE_USER_DATA_DIR", "")).absolute() != data:
        raise ValueError("Select the isolated data root explicitly in MEWCODE_USER_DATA_DIR")
    root = bounded_directory(args.fixture, must_exist=False)
    root.mkdir()
    helper = lpac.find_runtime_helper(require_protocol=execution.PROTOCOL)
    report = {"schema": "mewcode-native-recovery-probe-v1", "complete": False,
              "bundle_identity": installed_bundle_identity(helper.parent),
              "machine_guid": machine_guid(), "cases": []}
    try:
        for fault in ("lost_transport", "committed_ack_lost", "unfinished_publication"):
            fixture = root / fault
            workspace = fixture / "中文 workspace 空格"
            workspace.mkdir(parents=True)
            original = workspace / "a.txt"
            original.write_text("original", encoding="utf-8")
            checker = build_checker(workspace)
            scope = lpac.NativeScope.from_checker(checker)
            executor = AcceptanceExecutor(data, (fixture,))
            invoke, publish, event = lpac.invoke_helper, publication.publish_workspace, publication._event
            async def lost_transport(helper, envelope, cancel, on_receipt=None):
                # The real helper fully executes, writes its own durable proof
                # and closes. Drop only the host transport result/callback.
                actual = await invoke(helper, envelope, cancel)
                assert actual.cleanup_confirmed is True and actual.exit_code == 0, actual.render()
                raise RuntimeError("synthetic transport acknowledgement loss")
            def lost_commit(*a, **kw):
                publish(*a, **kw)
                raise RuntimeError("synthetic committed acknowledgement loss")
            def lost_event(folder, state, **fields):
                event(folder, state, **fields)
                if state == "applied":
                    raise RuntimeError("synthetic interruption before commit marker")
            if fault == "lost_transport":
                target, attribute, injected = lpac, "invoke_helper", lost_transport
            elif fault == "committed_ack_lost":
                target, attribute, injected = publication, "publish_workspace", lost_commit
            else:
                target, attribute, injected = publication, "_event", lost_event
            with patch.object(target, attribute, injected):
                result = await executor("Set-Content -LiteralPath 'a.txt' -Value 'candidate' -NoNewline",
                                        scope=scope, work_dir=str(workspace), timeout=30)
            assert result.is_error, "Fault injection did not interrupt the host"
            registry = TransactionRegistry(data / "native")
            record = registry.get(result.transaction_id)
            expected = "cleanup_unknown" if fault == "lost_transport" else "recovery_required"
            assert record["state"] == expected, result.render()
            assert workspace_write_block(workspace), "Unsettled transaction did not fence writes"
            proof = read_cleanup_evidence(Path(record["transaction"]) / "cleanup/receipt.json",
                request_id=record["request_id"], request_sha256=record["request_sha256"])
            # New host task, same explicit write scope. No edits to the old
            # task, journal, policy, receipt or immutable staged manifest.
            checker.set_task_authorization(TaskAuthorization.from_user_scope({
                "read_directories": [str(workspace)], "write_directories": [str(workspace)],
                "allowed_behaviors": ["isolated_command", "git_write"], "ttl_seconds": 3600,
            }, task_id="recovery-task"), task_id="recovery-task")
            current = lpac.NativeScope.from_checker(checker)
            assert execution.reconcile(current, registry)[0]["state"] == "recovery_required"
            offer = preview(current, registry, record["id"])
            outcome = execution.reconcile(current, registry, confirmation=offer)[0]
            expected = {"lost_transport": "rejected", "committed_ack_lost": "published",
                        "unfinished_publication": "rolled_back"}[fault]
            assert outcome["state"] == expected, outcome
            assert original.read_text() == ("candidate" if expected == "published" else "original")
            assert not workspace_write_block(workspace)
            saved = registry.get(record["id"])
            assert saved["policy_revision"] == record["policy_revision"]
            assert saved["task_id"] == record["task_id"]
            if fault == "lost_transport":
                assert saved["cleanup_evidence"] == proof
                assert "exit_code" not in saved["native_receipt"]
            report["cases"].append({"case": fault, "ok": True,
                "transaction_id": record["id"], "state": outcome["state"],
                "request_sha256": record["request_sha256"], "cleanup_confirmed": True,
                "handoff_receipt": saved["last_recovery_handoff"]})
            print(fault + ": passed", flush=True)
        report["complete"] = True
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        close_session()
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
