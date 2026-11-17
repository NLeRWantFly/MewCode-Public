"""User-authorized, synthetic-only live test of the handle-based SACL broker.

Only fresh fixtures under this repository's .tmp are accepted. No original
business file, system object, root ACL or persistent service is modified.
The host stays non-elevated; only the compiled bounded broker requests UAC.
"""
from __future__ import annotations

import argparse
import base64
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mewcode.sandbox.metadata_broker import transaction_scope, close_session, BrokerError
from mewcode.sandbox.publication_io import PinnedPath, FrozenSecurity
from mewcode.sandbox.lpac_runtime import checked_path


def from_sddl(sddl):
    api, kernel = C.WinDLL("advapi32", use_last_error=True), C.WinDLL("kernel32")
    convert = api.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes, convert.restype = [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p], W.BOOL
    length = api.GetSecurityDescriptorLength
    length.argtypes, length.restype = [C.c_void_p], W.DWORD
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [C.c_void_p], C.c_void_p
    raw = C.c_void_p()
    if not convert(sddl, 1, C.byref(raw), None):
        raise C.WinError(C.get_last_error())
    try:
        return C.string_at(raw, length(raw))
    finally:
        kernel.LocalFree(raw)


def run(executable, manifest, fixture, bundle=None):
    executable = checked_path(executable, file=True)
    manifest = json.loads(checked_path(manifest, file=True).read_text(encoding="utf-8"))
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    if digest != manifest.get("sha256"):
        raise ValueError("Broker manifest mismatch")
    fixture = fixture.absolute()
    checked_path(fixture.parent)
    if not fixture.is_relative_to(ROOT / ".tmp") or fixture.exists():
        raise ValueError("Use a new repository .tmp synthetic fixture")
    fixture.mkdir()
    workspace, private = fixture / "workspace", fixture / "stage-synthetic"
    workspace.mkdir()
    (private / "publication/incoming").mkdir(parents=True)
    (private / "publication/old").mkdir()
    (workspace / "original.txt").write_text("original untouched", encoding="utf-8")
    incoming = private / "publication/incoming/payload"
    incoming.write_text("candidate", encoding="utf-8")
    report = {"schema": "mewcode-metadata-broker-probe-v1", "helper_sha256": digest,
              "complete": False, "cases": [], "started": time.time()}
    if bundle is not None:
        from mewcode.sandbox.native_gate import installed_bundle_identity, machine_guid
        from scripts.manage_native_runtime import verify_bundle
        bundle = checked_path(bundle)
        if executable != bundle / "broker/MewCode.MetadataBroker.exe" or not verify_bundle(bundle)["ok"]:
            raise ValueError("Probe broker must belong to the verified acceptance bundle")
        report.update(bundle_identity=installed_bundle_identity(bundle), machine_guid=machine_guid())
    path = fixture / "report.json"
    def save():
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    try:
        with transaction_scope(workspace, private, executable=executable, sha256=digest) as broker:
            with PinnedPath(workspace / "original.txt", security_write=True) as source:
                original = broker.snapshot(source.handle, source.file_id)
                assert original["sacl"]["state"] == "captured"
                report["cases"].append({"case": "standard_parent_privileged_snapshot", "ok": True})
                try:
                    broker.request("register", handle=int(source.handle), file_id=list(source.file_id))
                except BrokerError:
                    report["cases"].append({"case": "original_registration_denied", "ok": True})
                else:
                    raise AssertionError("Original unexpectedly registered")
                try:
                    broker.snapshot(source.handle, (source.file_id[0], source.file_id[1] + 1))
                except BrokerError:
                    report["cases"].append({"case": "wrong_object_identity_denied", "ok": True})
                else:
                    raise AssertionError("Wrong identity accepted")
            plain_payload = private / "publication/incoming/plain"
            plain_payload.write_bytes(b"ordinary parent audit policy")
            with PinnedPath(workspace, directory=True) as parent:
                plain_expected = FrozenSecurity.inherited(parent, directory=False)
            with PinnedPath(plain_payload, movable=True, security_write=True) as plain:
                broker.apply(plain, plain_expected.security_descriptor())
                report["plain_audit_readback"] = {"expected": plain_expected.audit_signature(),
                                                   "private": plain.audit_signature()}
                plain.rename_absent(workspace / "plain.txt")
                broker.apply(plain, plain_expected.security_descriptor(), final=True)
                report["plain_audit_readback"]["published"] = plain.audit_signature()
                assert plain.audit_signature() == plain_expected.audit_signature()
            report["cases"].append({"case": "ordinary_parent_audit_policy_preserved", "ok": True})
            with PinnedPath(incoming, movable=True, security_write=True) as target:
                before = target.security_string(7)
                # A nonempty protected audit ACL on the synthetic payload only.
                audit = from_sddl(before + "S:P(AU;SAFA;0x120089;;;WD)")
                broker.apply(target, audit, final=True)
                observed = broker.snapshot(target.handle, target.file_id)
                assert observed["sacl"]["present"] is True
                assert "AU;SAFA" in observed["sacl"]["sddl"]
                assert target.security_string(7) == before
                report["cases"].append({"case": "nonempty_sacl_without_dacl_change", "ok": True})
                target.rename_absent(workspace / "published.txt")
                # Same registered object remains usable after no-replace rename.
                broker.apply(target, audit, final=True)
                assert target.audit_signature() == FrozenSecurity(audit).audit_signature()
                report["cases"].append({"case": "registered_identity_survives_rename", "ok": True})
            parent_payload = private / "publication/incoming/parent"
            parent_payload.mkdir()
            with PinnedPath(parent_payload, directory=True, movable=True, security_write=True) as parent:
                parent_audit = from_sddl(parent.security_string(7) + "S:P(AU;OICISAFA;0x120089;;;WD)")
                broker.apply(parent, parent_audit, final=True)
                parent.rename_absent(workspace / "audit-parent")
                broker.apply(parent, parent_audit, final=True)
                parent.pin_renamed_directory()
                expected = FrozenSecurity.inherited(parent, directory=False)
                child_payload = private / "publication/incoming/child"
                child_payload.write_bytes(b"inherited audit")
                with PinnedPath(child_payload, movable=True, security_write=True) as child:
                    child.copy_dacl_from(expected)
                    broker.apply(child, expected.security_descriptor())
                    child.rename_absent(workspace / "audit-parent/child.txt")
                    child.copy_dacl_from(expected, final=True)
                    broker.apply(child, expected.security_descriptor(), final=True)
                    report["inheritance_readback"] = {"expected": expected.audit_signature(),
                                                       "observed": child.audit_signature()}
                    assert child.audit_signature() == expected.audit_signature()
                    assert "AU;" in child.audit_signature()
                report["cases"].append({"case": "new_file_inherits_audit_policy", "ok": True})
                modified_payload = private / "publication/incoming/modified-child"
                modified_payload.write_bytes(b"modified content, same inherited audit")
                with PinnedPath(workspace / "audit-parent/child.txt", movable=True) as original_child:
                    saved_security = original_child.security_descriptor()
                    with PinnedPath(modified_payload, movable=True, security_write=True) as modified:
                        modified.copy_dacl_from(original_child)
                        broker.apply(modified, saved_security)
                        original_child.rename_absent(private / "publication/old/child")
                        modified.rename_absent(workspace / "audit-parent/child.txt")
                        modified.copy_dacl_from(FrozenSecurity(saved_security), final=True)
                        broker.apply(modified, saved_security, final=True)
                        assert modified.audit_signature() == FrozenSecurity(saved_security).audit_signature()
                report["cases"].append({"case": "modified_file_preserves_inherited_audit", "ok": True})
            # Model the real empty-directory restore interruption: original
            # removed, host-created replacement renamed, final SACL pending.
            original_dir_payload = private / "publication/incoming/original-dir"
            original_dir_payload.mkdir()
            with PinnedPath(workspace / "audit-parent", directory=True) as audit_parent:
                directory_expected = FrozenSecurity.inherited(audit_parent, directory=True)
            with PinnedPath(original_dir_payload, directory=True, movable=True, security_write=True) as original_dir:
                directory_audit = directory_expected.security_descriptor()
                broker.apply(original_dir, directory_audit, final=True)
                original_dir.rename_absent(workspace / "audit-parent/restored-dir")
                broker.apply(original_dir, directory_audit, final=True)
                assert original_dir.audit_signature() == directory_expected.audit_signature()
                original_id = list(original_dir.file_id)
                original_security = original_dir.security_descriptor()
        with transaction_scope(workspace, private, executable=executable, sha256=digest) as broker:
            (workspace / "audit-parent/restored-dir").rmdir()
            restore_payload = private / "publication/old/restore"
            restore_payload.mkdir()
            with PinnedPath(restore_payload, directory=True, movable=True, security_write=True) as restored:
                broker.apply(restored, original_security)
                restore_id = list(restored.file_id)
                restored.rename_absent(workspace / "audit-parent/restored-dir")
            recovery_plan = {"schema": "mewcode-staged-publication-v2", "source": str(workspace),
                "manifest_sha256": "a" * 64, "policy_revision": "b" * 64, "staged_digest": "c" * 64,
                "holder": {"owner_id": "synthetic"}, "lease_id": "synthetic", "metadata_broker_required": True,
                "entries": [{"kind": "directory", "operation": "delete", "relative": "audit-parent/restored-dir",
                             "old_id": original_id, "restore_id": restore_id, "original_security": original_security.hex()}]}
            (private / "publication/plan.json").write_text(json.dumps(recovery_plan), encoding="utf-8")
            assert (workspace / "original.txt").read_text(encoding="utf-8") == "original untouched"
        # A fresh scope discards every registration; recovery must rebind only
        # the plan's host-created restore identity, never an original object.
        with transaction_scope(workspace, private, executable=executable, sha256=digest) as broker:
            with PinnedPath(workspace / "audit-parent/restored-dir", directory=True, security_write=True) as restored:
                broker.register_recovery(restored, private / "publication")
                broker.apply(restored, original_security, final=True)
                assert restored.audit_signature() == FrozenSecurity(original_security).audit_signature()
                report["cases"].append({"case": "directory_audit_policy_restored", "ok": True})
                previous_sequence = broker.sequence
                broker.sequence -= 1
                try:
                    broker.snapshot(restored.handle, restored.file_id)
                except BrokerError as error:
                    assert "sequence mismatch" in str(error)
                    report["cases"].append({"case": "replayed_request_denied", "ok": True})
                else:
                    raise AssertionError("Replayed sequence accepted")
                finally:
                    broker.sequence = previous_sequence
                assert broker.snapshot(restored.handle, restored.file_id)["sacl"]["state"] == "captured"
        report.update(complete=True, ok=True)
    except BaseException as error:
        report.update(ok=False, error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        close_session()
        report["ended"] = time.time()
        save()
    return {"report": str(path), "ok": report["ok"], "cases": len(report["cases"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--bundle", type=Path, help="verified production bundle for release-bound evidence")
    args = parser.parse_args()
    print(json.dumps(run(args.helper, args.manifest, args.fixture, args.bundle)))
