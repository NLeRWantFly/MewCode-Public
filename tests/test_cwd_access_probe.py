"""Host-side compile and marker checks for the read-only Windows cwd probe."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest


MARKERS = (
    "getcwd",
    "dot:attributes",
    "parent:attributes",
    "dot_read_attributes:open",
    "dot_list_directory:open",
    "parent_list_directory:open",
    "dot_star:find",
    "parent_star:find",
    "git_mingw_getcwd",
    "git_mingw_getcwd_stage",
    "git_finalpath_open",
    "git_finalpath_dos_normalized",
    "git_finalpath_dos_opened",
    "git_finalpath_nt_normalized",
    "git_finalpath_nt_opened",
    "git_long_path_name",
    "drive_root_attributes",
    "drive_root_metadata_open",
    "volume_root_get",
    "volume_root_attributes",
    "volume_root_metadata_open",
)
STATUS = re.compile(r"(?:ok|failed:win32=\d+|failed:errno=ENOENT|buffer_too_small|not_executed)\Z")
STAGE_FIELDS = (
    "current_directory",
    "create_file",
    "final_path_normalized_dos",
    "long_path_fallback",
    "compat_attributes",
    "result",
    "scope",
)
ROUNDTRIP_KINDS = ("dos", "guid", "nt", "none")
ROUNDTRIP_FORMS = ("normalized", "opened")
ROUNDTRIP_STATUS = re.compile(
    r"(?:ok|failed:win32=\d+|buffer_too_small|not_executed)\Z"
)
ROUNDTRIP_KEYS = {"roundtrip_schema", "roundtrip_root_identity",
                  "roundtrip_parent_open", "roundtrip_parent_identity",
                  "roundtrip_parent_same_file"}
for _kind in ROUNDTRIP_KINDS:
    for _form in ROUNDTRIP_FORMS:
        ROUNDTRIP_KEYS.update(
            f"roundtrip_{_kind}_{_form}_{field}"
            for field in ("query", "reopen", "identity", "same_file")
        )
        if _kind == "none":
            ROUNDTRIP_KEYS.add(f"roundtrip_none_{_form}_paired_source")


@pytest.fixture(scope="module")
def probe_executable(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if os.name != "nt":
        pytest.skip("the probe P/Invokes Windows kernel32 APIs")
    root = Path(__file__).resolve().parents[1]
    system_root = Path(os.environ["SystemRoot"])
    compiler = system_root / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    source = root / "native/windows/MewCode.CwdAccessProbe.cs"
    build_dir = tmp_path_factory.mktemp("cwd-access-probe-build")
    executable = build_dir / "MewCode.CwdAccessProbe.exe"
    compiled = subprocess.run(
        [str(compiler), "/nologo", "/platform:x64", "/out:" + str(executable), str(source)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    return executable


def run_probe(executable: Path, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *arguments], cwd=cwd, capture_output=True,
        text=True, timeout=15, check=False,
    )


def parse_markers(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        assert separator, f"expected key=value marker, got {line!r}"
        assert key not in parsed, f"duplicate marker {key!r}"
        parsed[key] = value
    return parsed


def assert_complete_status_report(output: str) -> dict[str, str]:
    observed = parse_markers(output)
    assert set(observed) == set(MARKERS)
    for marker, value in observed.items():
        if marker == "git_mingw_getcwd_stage":
            fields = dict(part.split("=", 1) for part in value.split(";"))
            assert tuple(fields) == STAGE_FIELDS
            assert fields["scope"] == "win32_path_only"
            assert all(STATUS.fullmatch(fields[name]) for name in STAGE_FIELDS[:-1])
        else:
            assert STATUS.fullmatch(value), (marker, value)
    return observed


def assert_no_path_leak(output: str, *paths: Path) -> None:
    for path in paths:
        value = str(path)
        assert value not in output
        assert value.replace("\\", "/") not in output


def test_probe_reports_complete_statuses_from_empty_and_unicode_cwds(
    probe_executable: Path, tmp_path: Path,
) -> None:
    empty_cwd = tmp_path / "empty cwd"
    empty_cwd.mkdir()
    parent = empty_cwd.parent

    empty_result = run_probe(probe_executable, empty_cwd, str(parent))
    assert empty_result.returncode == 0, empty_result.stderr
    empty_markers = assert_complete_status_report(empty_result.stdout)
    assert empty_markers["getcwd"] == "ok"
    assert empty_markers["parent:attributes"] == "ok"
    assert empty_markers["git_mingw_getcwd"] == "ok"
    stage = dict(part.split("=", 1)
                 for part in empty_markers["git_mingw_getcwd_stage"].split(";"))
    assert stage["result"] == "ok"
    assert_no_path_leak(empty_result.stdout, empty_cwd, parent)

    unicode_cwd = tmp_path / "空 格 cwd Ω"
    unicode_cwd.mkdir()
    unicode_result = run_probe(probe_executable, unicode_cwd, str(parent))
    assert unicode_result.returncode == 0, unicode_result.stderr
    unicode_markers = assert_complete_status_report(unicode_result.stdout)
    assert unicode_markers["getcwd"] == "ok"
    assert unicode_markers["git_mingw_getcwd"] == "ok"
    assert_no_path_leak(unicode_result.stdout, unicode_cwd, parent)


def test_probe_reports_nonexistent_synthetic_parent_without_leaking_it(
    probe_executable: Path, tmp_path: Path,
) -> None:
    cwd = tmp_path / "synthetic cwd"
    cwd.mkdir()
    missing = tmp_path / "synthetic parent absent" / "missing"

    result = run_probe(probe_executable, cwd, str(missing))
    assert result.returncode == 0, result.stderr
    markers = assert_complete_status_report(result.stdout)
    assert re.fullmatch(r"failed:win32=\d+", markers["parent:attributes"])
    assert re.fullmatch(r"failed:win32=\d+", markers["parent_list_directory:open"])
    assert re.fullmatch(r"failed:win32=\d+", markers["parent_star:find"])
    assert_no_path_leak(result.stdout, cwd, missing)


def assert_roundtrip_report(output: str) -> dict[str, str]:
    observed = parse_markers(output)
    assert set(observed) == ROUNDTRIP_KEYS
    assert observed["roundtrip_schema"] == "mewcode-cwd-roundtrip-v1"
    for marker, value in observed.items():
        if marker == "roundtrip_schema":
            continue
        if marker.endswith("_same_file"):
            assert value in {"yes", "no", "not_executed"}, (marker, value)
        elif marker.endswith("_paired_source"):
            assert value in {"nt_normalized", "nt_opened", "not_executed"}
        else:
            assert ROUNDTRIP_STATUS.fullmatch(value), (marker, value)
    return observed


def test_roundtrip_v1_reopens_paths_from_unicode_space_host_directory(
    probe_executable: Path, tmp_path: Path,
) -> None:
    cwd = tmp_path / "宿主 中文 空 格 Ω"
    cwd.mkdir()
    synthetic_parent = cwd.parent

    result = run_probe(probe_executable, cwd, "--roundtrip-v1", str(synthetic_parent))
    assert result.returncode == 0, result.stderr
    markers = assert_roundtrip_report(result.stdout)
    assert markers["roundtrip_root_identity"] == "ok"
    assert markers["roundtrip_parent_open"] == "ok"
    assert markers["roundtrip_parent_identity"] == "ok"
    assert markers["roundtrip_parent_same_file"] == "no"
    for kind in ROUNDTRIP_KINDS:
        for form in ROUNDTRIP_FORMS:
            assert ROUNDTRIP_STATUS.fullmatch(markers[f"roundtrip_{kind}_{form}_query"])
            assert ROUNDTRIP_STATUS.fullmatch(markers[f"roundtrip_{kind}_{form}_reopen"])
            assert ROUNDTRIP_STATUS.fullmatch(markers[f"roundtrip_{kind}_{form}_identity"])
    for form in ROUNDTRIP_FORMS:
        assert markers[f"roundtrip_nt_{form}_query"] == "ok"
        assert markers[f"roundtrip_nt_{form}_reopen"] == "ok"
        assert markers[f"roundtrip_nt_{form}_identity"] == "ok"
        assert markers[f"roundtrip_nt_{form}_same_file"] == "yes"
    assert_no_path_leak(result.stdout, cwd, synthetic_parent)


def test_roundtrip_v1_preserves_missing_parent_open_failure(
    probe_executable: Path, tmp_path: Path,
) -> None:
    cwd = tmp_path / "roundtrip synthetic cwd"
    cwd.mkdir()
    missing = tmp_path / "synthetic missing parent" / "absent"

    result = run_probe(probe_executable, cwd, "--roundtrip-v1", str(missing))
    assert result.returncode == 0, result.stderr
    markers = assert_roundtrip_report(result.stdout)
    assert re.fullmatch(r"failed:win32=\d+", markers["roundtrip_parent_open"])
    assert markers["roundtrip_parent_identity"] == "not_executed"
    assert markers["roundtrip_parent_same_file"] == "not_executed"
    assert_no_path_leak(result.stdout, cwd, missing)


def test_probe_parameter_errors_and_pure_branch_selftest(probe_executable: Path, tmp_path: Path) -> None:
    cwd = tmp_path / "arguments"
    cwd.mkdir()
    no_arguments = run_probe(probe_executable, cwd)
    assert no_arguments.returncode == 2
    assert no_arguments.stdout == ""

    too_many_arguments = run_probe(probe_executable, cwd, "one", "two")
    assert too_many_arguments.returncode == 2
    assert too_many_arguments.stdout == ""

    missing_roundtrip_parent = run_probe(probe_executable, cwd, "--roundtrip-v1")
    assert missing_roundtrip_parent.returncode == 2
    assert missing_roundtrip_parent.stdout == ""

    invalid_roundtrip_arguments = run_probe(
        probe_executable, cwd, "--roundtrip-v1", str(cwd), "extra"
    )
    assert invalid_roundtrip_arguments.returncode == 2
    assert invalid_roundtrip_arguments.stdout == ""

    invalid_option = run_probe(probe_executable, cwd, "not-an-option", str(cwd))
    assert invalid_option.returncode == 2
    assert invalid_option.stdout == ""

    logic = run_probe(probe_executable, cwd, "--logic-selftest")
    assert logic.returncode == 0, logic.stderr
    assert parse_markers(logic.stdout) == {
        "logic_selftest": "ok",
        "logic_selftest_api_calls": "not_executed",
    }

    roundtrip_logic = run_probe(probe_executable, cwd, "--roundtrip-logic-selftest")
    assert roundtrip_logic.returncode == 0, roundtrip_logic.stderr
    assert parse_markers(roundtrip_logic.stdout) == {
        "roundtrip_logic_nt_shape": "ok",
        "roundtrip_logic_none_pairing": "ok",
        "roundtrip_logic_buffer_threshold": "ok",
        "roundtrip_logic_identity": "ok",
        "roundtrip_logic_selftest": "ok",
        "roundtrip_logic_selftest_api_calls": "not_executed",
    }
