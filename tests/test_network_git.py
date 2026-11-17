from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mewcode.sandbox import lpac_runtime, network_git
from mewcode.sandbox.windows import NativeBoundaryUnavailable


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def network_bundle(tmp_path, monkeypatch):
    data_root = tmp_path / "user-data"
    native = data_root / "native"
    core = native / "windows-lpac-v1"
    git = core / "git"
    git.mkdir(parents=True)

    helper = b"synthetic-native-runtime"
    git_exe = b"synthetic-bundled-git"
    broker = b"synthetic-metadata-broker"
    base_dlls = {name: ("synthetic-core-" + name).encode("ascii") for name in network_git.DLL_NAMES}
    (core / "MewCode.AppContainerRuntime.exe").write_bytes(helper)
    (git / "git.exe").write_bytes(git_exe)
    for name, raw in base_dlls.items():
        (git / name).write_bytes(raw)
    (core / "broker").mkdir()
    (core / "broker" / "MewCode.MetadataBroker.exe").write_bytes(broker)
    base_files = {
        "git/git.exe": _sha(git_exe),
        **{f"git/{name}": _sha(raw) for name, raw in base_dlls.items()},
        "broker/MewCode.MetadataBroker.exe": _sha(broker),
    }
    core_manifest = {
        "schema": "mewcode-lpac-bundle-v1",
        "sha256": _sha(helper),
        "production_enabled": False,
        "bundle_version": "synthetic.1",
        "protocols": ["mewcode-lpac-request-v1", "mewcode-metadata-broker-v1"],
        "files": base_files,
    }
    core_manifest_path = core / "manifest.json"
    core_manifest_path.write_text(json.dumps(core_manifest, sort_keys=True, indent=2) + "\n",
                                  encoding="utf-8")
    gate_bytes = {
        "attestation.json": b"synthetic original attestation\n",
        "enabled.json": b"synthetic original enablement\n",
        "recovery-policy.json": b"synthetic recovery policy\n",
    }
    for name, raw in gate_bytes.items():
        (core / name).write_bytes(raw)

    project = tmp_path / "project"
    sdk = project / network_git.SDK_BUILD
    (sdk / "bin").mkdir(parents=True)
    helper_source = project / network_git.SDK_HELPER_SOURCE
    helper_source.parent.mkdir(parents=True)
    helper_payload = b"synthetic-pinned-http-helper"
    helper_source.write_bytes(helper_payload)
    curl_payloads = {name: ("synthetic-network-" + name).encode("ascii")
                     for name in network_git.CURL_DLL_SHA256}
    for name, raw in curl_payloads.items():
        (sdk / "bin" / name).write_bytes(raw)
    ca_bundle = b"synthetic-pinned-ca-bundle"
    ca_source = sdk / "etc" / "ssl" / "certs" / network_git.CA_BUNDLE_NAME
    ca_source.parent.mkdir(parents=True)
    ca_source.write_bytes(ca_bundle)

    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(data_root))
    monkeypatch.setattr(network_git, "ROOT", project)
    monkeypatch.setattr(network_git, "SDK_HELPER_SHA256", _sha(helper_payload))
    monkeypatch.setattr(network_git, "BASE_DLL_SHA256", {name: _sha(raw) for name, raw in base_dlls.items()})
    monkeypatch.setattr(network_git, "CURL_DLL_SHA256", {name: _sha(raw) for name, raw in curl_payloads.items()})
    monkeypatch.setattr(network_git, "CA_BUNDLE_SHA256", _sha(ca_bundle))
    return {
        "data_root": data_root,
        "native": native,
        "core": core,
        "core_manifest": core_manifest_path,
        "core_manifest_bytes": core_manifest_path.read_bytes(),
        "gate_bytes": gate_bytes,
        "core_payloads": {"git/git.exe": git_exe,
                           **{f"git/{name}": raw for name, raw in base_dlls.items()}},
        "core_base_files": {name: base_files[name] for name in base_files if name.startswith("git/")},
        "helper_payload": helper_payload,
        "helper_source": helper_source,
        "sdk": sdk,
        "curl_payloads": curl_payloads,
        "ca_payload": ca_bundle,
        "ca_source": ca_source,
        "network_root": native / network_git.PACKAGE_NAME,
    }


def _core_bytes(case):
    core = case["core"]
    return {path.relative_to(core).as_posix(): path.read_bytes()
            for path in core.rglob("*") if path.is_file()}


def _assert_core_unchanged(case, before):
    assert _core_bytes(case) == before


def test_fixed_sdk_locations_and_hashes_are_preserved():
    assert network_git.SDK_BUILD == ".tmp/git-auto-compat-20260926/sdk/mingw64"
    assert network_git.SDK_HELPER_SOURCE == ".tmp/git-auto-compat-20260926/patched/git-remote-http.exe"
    assert network_git.SDK_HELPER_SHA256 == "670dcd991d43d489ee1ef110139f74c4a4de44a3ca7ae91b3af44b0bbcb9619c"
    assert network_git.BASE_DLL_SHA256 == {
        "libiconv-2.dll": "7a282a854e01be726c6cccfe46f548c716aa45b3014818468253aaa4efbcd067",
        "libintl-8.dll": "0537c3dd2378218508ebe3cc416d72a99ee2d24ae1c5525e23458f32544ef861",
        "libpcre2-8-0.dll": "ed6f43f96b0891a5b470e724f727bb4c12255354e609b40bd81fd5be6c0b56ac",
        "zlib1.dll": "93e9243a44c29200eeacaf9658efe2558581770e4b11ca4b500e18e424a6e3b5",
    }
    assert network_git.CURL_DLL_SHA256["libcurl-openssl-4.dll"] == "b2ce87aab267f5009803585944e08c17d94a27683ed875294508b8bbb45244dc"
    assert network_git.CURL_DLL_SHA256["libcrypto-3-x64.dll"] == "0330b5f558996f297d687e1a2b2fcc2cacf883b16baef74aaef35285d7c1231c"
    assert network_git.CURL_DLL_SHA256["libssl-3-x64.dll"] == "feb5b300e0b3a021fed481178f3d66896426576d4d13b85a304d2a3809b25bfd"
    assert network_git.CURL_DLL_SHA256["libnghttp2-14.dll"] == "25e6516899fd5021df8c67b92e9f7246249fb03bb7bdf56a08260a8dded06b00"
    assert network_git.CA_BUNDLE_SHA256 == "f345ac0d7dbd1a584dd80ff96eb2919e8dac68e962db2e7111401b434e888493"
    assert len(network_git._expected_target_hashes()) == 15


def test_installs_separate_exact_twenty_leaf_package_without_changing_core(network_bundle):
    case = network_bundle
    core_before = _core_bytes(case)
    result = network_git.ensure_https_helpers()

    assert result["configured"] is True and result["changed"] is True
    assert Path(result["directory"]) == case["network_root"] / "git"
    assert set(result) >= {"configured", "changed", "directory", "helpers", "sha256"}
    package_paths = network_git.paths()
    assert len(package_paths) == 21  # manifest plus twenty declared leaves
    manifest_path = case["network_root"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest) == {"schema", "base_bundle_identity", "files"}
    assert manifest["schema"] == "mewcode-network-git-v1"
    from mewcode.sandbox.native_gate import installed_bundle_identity
    assert manifest["base_bundle_identity"] == installed_bundle_identity(case["core"])
    assert len(manifest["files"]) == 20
    expected = {**case["core_base_files"], **network_git._expected_target_hashes()}
    assert manifest["files"] == expected
    for relative, raw in {**case["core_payloads"],
                          "git/git-remote-http.exe": case["helper_payload"],
                          "git/git-remote-https.exe": case["helper_payload"],
                          **{f"git/{name}": raw for name, raw in case["curl_payloads"].items()},
                          f"git/{network_git.CA_BUNDLE_NAME}": case["ca_payload"]}.items():
        assert (case["network_root"] / relative).read_bytes() == raw
    assert {path.name for path in case["network_root"].iterdir()} == {"manifest.json", "git"}
    assert {path.name for path in (case["network_root"] / "git").iterdir()} == {
        relative.removeprefix("git/") for relative in expected}
    _assert_core_unchanged(case, core_before)
    assert not list(case["native"].glob(f".{network_git.PACKAGE_NAME}.staging-*"))


def test_existing_package_is_idempotent_without_sdk_sources(network_bundle, monkeypatch):
    case = network_bundle
    first = network_git.ensure_https_helpers()
    before = {path: path.read_bytes() for path in network_git.paths()}
    monkeypatch.setattr(network_git, "SDK_BUILD", "missing/sdk")
    monkeypatch.setattr(network_git, "SDK_HELPER_SOURCE", "missing/helper.exe")

    result = network_git.ensure_https_helpers()

    assert first["changed"] is True
    assert result["configured"] is True and result["changed"] is False
    assert {path: path.read_bytes() for path in network_git.paths()} == before
    assert Path(result["directory"]) == case["network_root"] / "git"


@pytest.mark.parametrize(
    ("source_kind", "error_text"),
    [
        ("helper", "pinned SDK payload hash mismatch for patched Git HTTP helper"),
        ("dll", "pinned SDK payload hash mismatch for libssl-3-x64.dll"),
        ("ca", "pinned SDK payload hash mismatch for CA bundle"),
    ],
)
def test_refuses_drifted_fixed_sdk_source_before_staging(network_bundle, source_kind, error_text):
    case = network_bundle
    core_before = _core_bytes(case)
    if source_kind == "helper":
        source = case["helper_source"]
    elif source_kind == "dll":
        source = case["sdk"] / "bin" / "libssl-3-x64.dll"
    else:
        source = case["ca_source"]
    source.write_bytes(b"drifted fixed source")

    with pytest.raises(NativeBoundaryUnavailable, match=error_text):
        network_git.ensure_https_helpers()

    assert not case["network_root"].exists()
    assert not list(case["native"].glob(f".{network_git.PACKAGE_NAME}.staging-*"))
    _assert_core_unchanged(case, core_before)


@pytest.mark.parametrize("damage", ["payload-drift", "unknown-root-file", "unknown-git-file"])
def test_refuses_existing_drifted_or_unknown_package_without_overwrite(network_bundle, damage,
                                                                       monkeypatch):
    case = network_bundle
    network_git.ensure_https_helpers()
    manifest_before = (case["network_root"] / "manifest.json").read_bytes()
    if damage == "payload-drift":
        leaf = case["network_root"] / "git" / "git-remote-http.exe"
        leaf.write_bytes(b"drifted installed payload")
        old_leaf = leaf.read_bytes()
    else:
        leaf = case["network_root"] / ("unknown.bin" if damage == "unknown-root-file"
                                        else "git/unknown.bin")
        leaf.write_bytes(b"unknown leaf")
        old_leaf = leaf.read_bytes()
    core_before = _core_bytes(case)
    monkeypatch.setattr(network_git, "SDK_BUILD", "missing/sdk")
    monkeypatch.setattr(network_git, "SDK_HELPER_SOURCE", "missing/helper.exe")

    with pytest.raises(NativeBoundaryUnavailable):
        network_git.ensure_https_helpers()

    assert leaf.read_bytes() == old_leaf
    assert (case["network_root"] / "manifest.json").read_bytes() == manifest_before
    _assert_core_unchanged(case, core_before)


def test_refuses_network_package_bound_to_changed_core_identity(network_bundle):
    case = network_bundle
    network_git.ensure_https_helpers()
    package_before = {path: path.read_bytes() for path in network_git.paths()}
    broker = case["core"] / "broker" / "MewCode.MetadataBroker.exe"
    broker.write_bytes(b"changed core broker")
    manifest = json.loads(case["core_manifest"].read_text(encoding="utf-8"))
    manifest["files"]["broker/MewCode.MetadataBroker.exe"] = _sha(broker.read_bytes())
    case["core_manifest"].write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n",
                                     encoding="utf-8")

    with pytest.raises(NativeBoundaryUnavailable, match="different core bundle identity"):
        network_git.ensure_https_helpers()
    with pytest.raises(NativeBoundaryUnavailable, match="different core bundle identity"):
        network_git.paths()
    assert {path: path.read_bytes() for path in package_before} == package_before


def test_refuses_to_reconfigure_when_core_contains_any_supplemental_leaf(network_bundle):
    case = network_bundle
    leaf = case["core"] / "git" / network_git.HELPER_NAMES[0]
    payload = b"old in-core supplement"
    leaf.write_bytes(payload)
    manifest = json.loads(case["core_manifest"].read_text(encoding="utf-8"))
    manifest["files"][f"git/{leaf.name}"] = _sha(payload)
    case["core_manifest"].write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n",
                                     encoding="utf-8")

    with pytest.raises(NativeBoundaryUnavailable, match="core already contains network payload leaves"):
        network_git.ensure_https_helpers()

    assert not case["network_root"].exists()


def test_refuses_unlisted_residual_supplement_in_core_tree(network_bundle):
    case = network_bundle
    residual = case["core"] / "git" / "libcurl-openssl-4.dll"
    residual.write_bytes(b"unlisted old in-core supplement")

    with pytest.raises(NativeBoundaryUnavailable, match="residual network payload leaf"):
        network_git.ensure_https_helpers()

    assert not case["network_root"].exists()


def test_runtime_directory_is_fixed_and_read_only(network_bundle):
    case = network_bundle
    assert network_git.runtime_git_directory() == case["network_root"] / "git"
    assert not case["network_root"].exists()


def test_interruption_before_directory_rename_leaves_only_unpublished_stage(network_bundle,
                                                                             monkeypatch):
    case = network_bundle
    core_before = _core_bytes(case)
    real_rename = network_git.os.rename

    def interrupt_before_publish(source, target):
        if Path(target) == case["network_root"]:
            raise KeyboardInterrupt("synthetic interruption before publish")
        return real_rename(source, target)

    monkeypatch.setattr(network_git.os, "rename", interrupt_before_publish)
    with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
        network_git.ensure_https_helpers()

    assert not case["network_root"].exists()
    stages = list(case["native"].glob(f".{network_git.PACKAGE_NAME}.staging-*"))
    assert len(stages) == 1
    assert len(network_git._validate_package(stages[0])) == 21
    _assert_core_unchanged(case, core_before)

    monkeypatch.setattr(network_git.os, "rename", real_rename)
    result = network_git.ensure_https_helpers()
    assert result["changed"] is True
    assert case["network_root"].is_dir()
    assert stages[0].is_dir()  # interrupted stages are retained for diagnosis
    _assert_core_unchanged(case, core_before)


def test_interruption_after_single_directory_commit_retries_idempotently(network_bundle,
                                                                          monkeypatch):
    case = network_bundle
    core_before = _core_bytes(case)
    real_rename = network_git.os.rename

    def interrupt_after_publish(source, target):
        result = real_rename(source, target)
        if Path(target) == case["network_root"]:
            raise SystemExit(89)
        return result

    monkeypatch.setattr(network_git.os, "rename", interrupt_after_publish)
    with pytest.raises(SystemExit):
        network_git.ensure_https_helpers()
    assert case["network_root"].is_dir()

    monkeypatch.setattr(network_git.os, "rename", real_rename)
    monkeypatch.setattr(network_git, "SDK_BUILD", "missing/sdk")
    monkeypatch.setattr(network_git, "SDK_HELPER_SOURCE", "missing/helper.exe")
    result = network_git.ensure_https_helpers()
    assert result["configured"] is True and result["changed"] is False
    assert not list(case["native"].glob(f".{network_git.PACKAGE_NAME}.staging-*"))
    _assert_core_unchanged(case, core_before)


@pytest.mark.parametrize("reason", ["reparse", "hardlink"])
def test_paths_refuses_reparse_or_hardlinked_payload(network_bundle, monkeypatch, reason):
    case = network_bundle
    network_git.ensure_https_helpers()
    target_leaf = case["network_root"] / "git" / network_git.HELPER_NAMES[0]
    real_checked = lpac_runtime.checked_path

    def reject_target(path, *, file=False):
        if Path(path) == target_leaf:
            if reason == "reparse":
                raise NativeBoundaryUnavailable("Native scope contains a reparse path")
            raise NativeBoundaryUnavailable("Native scope contains a hard-linked file")
        return real_checked(path, file=file)

    monkeypatch.setattr(lpac_runtime, "checked_path", reject_target)
    expected = "reparse path" if reason == "reparse" else "hard-linked file"
    with pytest.raises(NativeBoundaryUnavailable, match=expected):
        network_git.paths()
