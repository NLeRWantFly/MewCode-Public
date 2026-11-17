from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native/windows/MewCode.MetadataBroker.cs"
BUILDER_PATH = ROOT / "scripts/build_metadata_broker.py"
SPEC = importlib.util.spec_from_file_location("metadata_broker_builder", BUILDER_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def test_source_enforces_handle_only_bounded_protocol_and_sacl_only_apply():
    source = SOURCE.read_text(encoding="utf-8")
    assert "NamedPipeClientStream" in source
    assert "GetNamedPipeServerProcessId" in source
    assert "DuplicateSameAccess" in source
    assert "ReOpenFile(original.Handle, desiredAccess, 7, flags)" in source
    assert "if (original.IsDirectory)" in source
    assert "Native.OpenFileById(original.Handle, ref fileId, desiredAccess, 7, IntPtr.Zero, flags)" in source
    assert "[StructLayout(LayoutKind.Explicit, Size = 24)]" in source
    assert "internal struct FileIdDescriptor" in source
    assert "fileId.FileId = unchecked((long)original.Index);" in source
    assert "EntryPoint = \"OpenFileById\"" in source
    assert '"frame header read", LifetimeMilliseconds' in source
    assert '"frame body read", IoTimeoutMilliseconds' in source
    assert '"sddl", !descriptor.SaclPresent || descriptor.SaclNull ? null' in source
    assert "NativeObject persistent = ReopenForSecurity(file, granted)" in source
    assert "NtQueryObject" in source
    assert "PublicObjectBasicInformation" in source
    assert "public uint Reserved9;" in source
    assert "Native.NtQueryObject(handle, 0, out information, capacity, out returned)" in source
    assert "capacity != 56 || returned < 8 || returned > capacity" in source
    assert '", returned=" + returned + ", capacity=" + capacity' in source
    assert 'int error = Marshal.GetLastWin32Error();' in source
    assert 'string annotated = message + " (Win32 error " + error + ", 0x" + error.ToString("X8")' in source
    assert 'desiredAccess=0x' in source and 'flags=0x' in source
    assert "IsValidSecurityDescriptor" in source
    assert "ReadControl" in source
    assert "WriteDac" in source
    assert "AccessSystemSecurity" in source
    assert "ordinaryAccess | AccessSystemSecurity" in source
    assert "Native.NtSetSecurityObject(handle, information" in source
    assert "SaclSecurityInformation |" in source
    assert "RemoveInheritedSaclAces" not in source
    assert "explicitAces" not in source
    assert "bool protectedSacl = final ? (control & SaclProtected) != 0 : true;" in source
    assert "if (!descriptor.SaclPresent)" in source
    assert "DescriptorParts current = ReadDescriptor(handle);" in source
    assert "bool originalProtection = (descriptor.Control & SaclProtected) != 0;" in source
    assert "if (!current.SaclPresent && currentProtection == originalProtection)\n                    return;" in source
    assert 'op == "register_recovery"' in source
    assert "MaximumPlanBytes = 8 * 1024 * 1024" in source
    assert "mewcode-staged-publication-v2" in source
    assert "RecoveryDescriptors.TryGetValue(file.Identity" in source
    assert "MaximumFrame = 256 * 1024" in source
    assert "LifetimeMilliseconds = 1200000" in source
    assert "SeSecurityPrivilege" in source
    assert "SeBackupPrivilege" not in source
    assert "SeRestorePrivilege" not in source
    assert "SeDebugPrivilege" not in source
    assert "Process.Start" not in source
    assert "cmd.exe" not in source.lower()
    assert "python.exe" not in source.lower()


def test_build_requires_new_empty_output_directory(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        builder.build(existing)
    assert list(existing.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="the .NET Framework compiler is Windows-only")
def test_build_creates_exe_and_sha256_manifest_in_new_directory(tmp_path):
    output = tmp_path / "metadata-broker"
    manifest = builder.build(output)
    executable = output / "MewCode.MetadataBroker.exe"
    manifest_path = output / "manifest.json"

    assert executable.is_file()
    assert manifest_path.is_file()
    assert sorted(item.name for item in output.iterdir()) == ["MewCode.MetadataBroker.exe", "manifest.json"]
    disk_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert disk_manifest == manifest
    assert manifest["schema"] == "mewcode-metadata-broker-v1"
    assert manifest["protocol"] == "length-prefixed-utf8-json-v1"
    assert manifest["production_enabled"] is False
    assert manifest["sha256"] == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert manifest["source_sha256"] == hashlib.sha256(SOURCE.read_bytes()).hexdigest()
