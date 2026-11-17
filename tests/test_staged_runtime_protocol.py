from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import build_windows_lpac


ROOT = Path(__file__).resolve().parents[1]
STAGED_SCHEMA = "mewcode-lpac-staged-request-v1"
PROTOTYPE_SCHEMA = "mewcode-lpac-staged-prototype-v1"


def read_source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_runtime_has_a_distinct_staged_envelope_and_keeps_legacy_entry():
    runtime = read_source("native/windows/MewCode.AppContainerRuntime.cs")
    staged = read_source("native/windows/MewCode.StagedWorkspacePrototype.cs")

    assert "public sealed class StagedRuntimeRequestEnvelope" in runtime
    for field in ("request_id", "timeout_ms", "RuntimeRequest request", "readonly_paths",
                  "pinned_containers", "original_workspace"):
        assert field in runtime
    assert f'"{STAGED_SCHEMA}"' in staged
    assert f'"{PROTOTYPE_SCHEMA}"' in staged
    assert "if (staged != null && staged.schema == StagedRuntimeSchema)" in runtime
    assert "ValidateRuntimeRequest(request)" in runtime
    assert "MewCodeStagedWorkspacePrototype" in staged
    assert "public static int Main(string[] args)" in staged


def test_staged_protocol_checks_original_grants_protections_and_bounded_tree():
    staged = read_source("native/windows/MewCode.StagedWorkspacePrototype.cs")
    probe = read_source("native/windows/MewCode.AppContainerProbe.cs")

    assert "ValidateStagedWorkspaceRoot(workspace)" in staged
    assert '"stage-"' in staged and '"workspace"' in staged
    assert "ValidateNoOriginalWorkspaceGrants(original, validated)" in staged
    assert "An exact grant targets the original workspace." in staged
    assert "An exact grant is an ancestor of the original workspace." in staged
    assert "AddStagedProtected(envelope.readonly_paths" in staged
    assert "AddStagedProtected(envelope.pinned_containers" in staged
    assert "paths.Length > MaxStagedEntries" in staged
    assert "RejectHardLinkedFile(path, \"staged file\")" in staged
    assert "ValidateStagedTree(workspace)" in staged
    assert "ValidateStagedTree(request.StagedWorkspace)" in probe
    assert "if (state.ProcessCreated && !state.JobEmptyConfirmed)" in probe
    assert "#if MEWCODE_STAGED_PROTOTYPE" not in staged
    assert "#if MEWCODE_STAGED_PROTOTYPE" not in probe


def test_private_runtime_files_can_reopen_without_granting_real_profile():
    staged = read_source("native/windows/MewCode.StagedWorkspacePrototype.cs")
    probe = read_source("native/windows/MewCode.AppContainerProbe.cs")
    assert 'string[] names = { "TEMP", "USERPROFILE" }' in staged
    assert "validated.StagedRuntimeRoots = roots" in staged
    assert "foreach (string root in request.StagedRuntimeRoots) ValidateStagedTree(root)" in probe


def test_standard_builder_compiles_and_advertises_staged_protocol(tmp_path):
    if os.name != "nt":
        pytest.skip("The standard LPAC bundle compiler is Windows-only")

    output = tmp_path / "fresh-bundle"
    manifest = build_windows_lpac.build(output)

    assert (output / "MewCode.AppContainerRuntime.exe").is_file()
    saved_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert saved_manifest == manifest
    assert saved_manifest["production_enabled"] is False
    assert saved_manifest["protocols"] == ["mewcode-lpac-request-v1", STAGED_SCHEMA, "mewcode-native-cleanup-v1"]
    assert "MewCode.StagedWorkspacePrototype.cs" in saved_manifest["source_sha256"]
