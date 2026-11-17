import os
import subprocess
from pathlib import Path

import pytest

from mewcode.sandbox import project_python


def _relative_interpreter(env_name=".venv"):
    suffix = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return Path(env_name) / suffix


def _fake_pymupdf(site_packages: Path, *, extra_files=(), symlink_file=None):
    dist_name = "PyMuPDF-99.1.dist-info"
    package = site_packages / "pymupdf"
    metadata = site_packages / dist_name
    package.mkdir(parents=True)
    metadata.mkdir()
    (package / "__init__.py").write_text(
        "from pathlib import Path\nPath(__file__).with_name('IMPORTED').touch()\n", encoding="utf-8")
    (package / "_native.pyd").write_bytes(b"offline extension fixture")
    (package / "ignored.pyc").write_bytes(b"cache")
    (package / "ignored.pth").write_text("import os\n", encoding="utf-8")
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: PyMuPDF\nVersion: 99.1\n\n", encoding="utf-8")
    (metadata / "top_level.txt").write_text("pymupdf\n", encoding="utf-8")

    files = [
        "pymupdf/__init__.py", "pymupdf/_native.pyd", "pymupdf/ignored.pyc", "pymupdf/ignored.pth",
        f"{dist_name}/METADATA", f"{dist_name}/top_level.txt", f"{dist_name}/RECORD",
        "../../Scripts/pymupdf.exe", "Scripts/helper.exe",
    ]
    for relative in extra_files:
        files.append(relative)
    if symlink_file:
        link = site_packages / symlink_file
        link.parent.mkdir(parents=True, exist_ok=True)
        target = site_packages.parent / "outside.py"
        target.write_text("outside", encoding="utf-8")
        os.symlink(target, link)
        files.append(symlink_file)
    rows = "".join(f"{name},,\n" for name in files)
    (metadata / "RECORD").write_text(rows, encoding="utf-8")
    return package, metadata


def _fake_distribution(site_packages: Path, name: str, version: str, *, module: str,
                       requires=(), top_level=True):
    dist_info = site_packages / f"{name.replace('-', '_')}-{version}.dist-info"
    package = site_packages / module
    dist_info.mkdir(parents=True)
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text(f"VALUE = {name!r}\n", encoding="utf-8")
    detail = package / "detail.py"
    detail.write_text("DETAIL = True\n", encoding="utf-8")
    (package / "ignored.pyc").write_bytes(b"cache")
    (package / "ignored.pth").write_text("import os\n", encoding="utf-8")
    metadata = (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires) + "\n")
    (dist_info / "METADATA").write_text(metadata, encoding="utf-8")
    if top_level:
        (dist_info / "top_level.txt").write_text(module + "\n", encoding="utf-8")
    records = [f"{module}/__init__.py,,", f"{module}/detail.py,,",
               f"{module}/ignored.pyc,,", f"{module}/ignored.pth,,",
               f"{dist_info.name}/METADATA,,"]
    if top_level:
        records.append(f"{dist_info.name}/top_level.txt,,")
    records.extend([f"{dist_info.name}/RECORD,,", "../../Scripts/external-command.exe,,"])
    (dist_info / "RECORD").write_text("\n".join(records) + "\n", encoding="utf-8")
    return package, dist_info


def _use_host_site_packages(monkeypatch, site_packages):
    original = project_python.sysconfig.get_paths

    def get_paths(scheme=None, vars=None, expand=True):
        paths = original() if scheme is None and vars is None else original(scheme, vars, expand)
        if vars is None and scheme is None:
            paths = dict(paths)
            paths["purelib"] = str(site_packages)
            paths["platlib"] = str(site_packages)
        return paths

    monkeypatch.setattr(project_python.sysconfig, "get_paths", get_paths)


def test_inspect_missing_and_empty_creation_never_launches_subprocess(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    before = project_python.inspect_project_python(project)
    assert before["status"] == "missing" and before["available"] is False
    assert before["validation_required"] is True

    def forbidden(*_args, **_kwargs):
        raise AssertionError("project Python preparation must not launch a process")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    result = project_python.prepare_project_python(project)
    assert result["ok"] is True and result["prepared"] is True
    assert result["ready"] is False and result["validation_required"] is True
    assert result["interpreter"] == _relative_interpreter().as_posix()
    assert project.joinpath(*_relative_interpreter().parts).is_file()
    assert not (project / ".venv" / project_python._PREPARING_MARKER).exists()
    inspected = project_python.inspect_project_python(project)
    assert inspected["status"] == "available" and inspected["available"] is True
    assert inspected["interpreter"] == result["interpreter"]
    assert inspected["validation_required"] is True


@pytest.mark.parametrize("existing_name", [".venv", "venv"])
def test_existing_environment_is_never_modified(tmp_path, existing_name):
    project = tmp_path / "project"
    project.mkdir()
    existing = project / existing_name
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("user environment", encoding="utf-8")

    result = project_python.prepare_project_python(project)
    assert result["ok"] is False and result["prepared"] is False
    assert result["error"]["code"] == "environment_exists"
    assert sentinel.read_text(encoding="utf-8") == "user environment"
    assert not (project / ("venv" if existing_name == ".venv" else ".venv")).exists()


def test_unsupported_package_is_rejected_before_creating_environment(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    result = project_python.prepare_project_python(project, packages=("not-installed-fixture-package",))
    assert result["ok"] is False
    assert result["error"]["code"] == "package_not_installed"
    assert not (project / ".venv").exists()


def test_malicious_manifest_path_is_rejected_before_creating_environment(tmp_path, monkeypatch):
    project = tmp_path / "project"
    site_packages = tmp_path / "host" / "site-packages"
    project.mkdir()
    site_packages.mkdir(parents=True)
    _fake_pymupdf(site_packages)
    _use_host_site_packages(monkeypatch, site_packages)
    metadata = site_packages / "PyMuPDF-99.1.dist-info" / "RECORD"
    metadata.write_text(metadata.read_text(encoding="utf-8") + "../escape.py,,\n", encoding="utf-8")

    result = project_python.prepare_project_python(project, packages=("pymupdf",))
    assert result["ok"] is False
    assert result["error"]["code"] == "unsafe_package_path"
    assert not (project / ".venv").exists()


def test_non_pdf_distribution_copies_recursive_requires_dist_closure_offline(tmp_path, monkeypatch):
    project = tmp_path / "project"
    site_packages = tmp_path / "host" / "site-packages"
    project.mkdir()
    site_packages.mkdir(parents=True)
    _fake_distribution(site_packages, "sample-app", "1.0", module="sample_app",
                       requires=("helper-lib>=1,<2; python_version >= '3.11'",
                                 "optional-helper; extra == 'speed'",
                                 "not-on-this-python; python_version < '3.0'"))
    _fake_distribution(site_packages, "helper-lib", "1.5", module="helper_lib",
                       requires=("leaf-lib==3.2",))
    _fake_distribution(site_packages, "leaf-lib", "3.2", module="leaf_lib")
    _use_host_site_packages(monkeypatch, site_packages)

    result = project_python.prepare_project_python(project, packages=("Sample_App",))

    assert result["ok"] is True and result["prepared"] is True
    assert set(result["packages"]) == {"sample-app", "helper-lib", "leaf-lib"}
    assert result["packages"]["helper-lib"]["version"] == "1.5"
    destination = project_python._destination_site_packages(project / ".venv")
    assert (destination / "sample_app" / "__init__.py").is_file()
    assert (destination / "helper_lib" / "detail.py").is_file()
    assert (destination / "leaf_lib" / "__init__.py").is_file()
    assert not (destination / "optional_helper").exists()
    assert not (destination / "not_on_this_python").exists()
    assert not (destination / "sample_app" / "ignored.pyc").exists()
    assert not (destination / "sample_app" / "ignored.pth").exists()
    assert not (project / ".venv" / "Scripts" / "external-command.exe").exists()


@pytest.mark.parametrize(("requires", "dep_version", "expected_code"), [
    (("missing-helper>=1",), None, "dependency_not_installed"),
    (("helper-lib>=2",), "1.5", "dependency_version_mismatch"),
    (("helper-lib[fast]>=1",), "1.5", "unsupported_extra"),
    (("helper-lib @ https://invalid.example/helper.whl",), "1.5", "unsupported_dependency"),
])
def test_dependency_preflight_failures_are_atomic(tmp_path, monkeypatch, requires, dep_version, expected_code):
    project = tmp_path / "project"
    site_packages = tmp_path / "host" / "site-packages"
    project.mkdir()
    site_packages.mkdir(parents=True)
    _fake_distribution(site_packages, "sample-app", "1.0", module="sample_app", requires=requires)
    if dep_version:
        _fake_distribution(site_packages, "helper-lib", dep_version, module="helper_lib")
    _use_host_site_packages(monkeypatch, site_packages)

    result = project_python.prepare_project_python(project, packages=("sample-app",))

    assert result["ok"] is False
    assert result["error"]["code"] == expected_code
    assert not (project / ".venv").exists()


def test_distribution_name_validation_is_strict_and_alias_duplicates_are_rejected(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    unsafe = project_python.prepare_project_python(project, packages=("requests; os.system('bad')",))
    assert unsafe["error"]["code"] == "invalid_package_name"
    duplicate = project_python.prepare_project_python(project, packages=("some_pkg", "some.pkg"))
    assert duplicate["error"]["code"] == "duplicate_package"
    assert not (project / ".venv").exists()


def test_symlinked_package_source_is_rejected_before_creating_environment(tmp_path, monkeypatch):
    project = tmp_path / "project"
    site_packages = tmp_path / "host" / "site-packages"
    project.mkdir()
    site_packages.mkdir(parents=True)
    try:
        _fake_pymupdf(site_packages, symlink_file="pymupdf/external.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation is unavailable on this host: {exc}")
    _use_host_site_packages(monkeypatch, site_packages)

    result = project_python.prepare_project_python(project, packages=("pymupdf",))
    assert result["ok"] is False
    assert result["error"]["code"] == "unsafe_link"
    assert not (project / ".venv").exists()


def test_target_symlink_is_refused_without_touching_its_target(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside-environment"
    project.mkdir()
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    try:
        os.symlink(outside, project / ".venv", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation is unavailable on this host: {exc}")

    result = project_python.prepare_project_python(project)
    assert result["ok"] is False and result["status"] == "invalid"
    assert sentinel.read_text(encoding="utf-8") == "untouched"


def test_pymupdf_is_copied_offline_without_import_or_script_cache_copy(tmp_path, monkeypatch):
    project = tmp_path / "project"
    site_packages = tmp_path / "host" / "site-packages"
    project.mkdir()
    site_packages.mkdir(parents=True)
    _fake_pymupdf(site_packages)
    _use_host_site_packages(monkeypatch, site_packages)

    result = project_python.prepare_project_python(project, packages=("pymupdf",))
    assert result["ok"] is True and result["prepared"] is True
    assert result["ready"] is False and result["validation_required"] is True
    assert result["packages"]["pymupdf"]["version"] == "99.1"
    assert result["packages"]["pymupdf"]["copied_files"] >= 5
    destination = project_python._destination_site_packages(project / ".venv")
    copied_package = destination / "pymupdf"
    assert (copied_package / "__init__.py").is_file()
    assert (copied_package / "_native.pyd").read_bytes() == b"offline extension fixture"
    assert not (copied_package / "ignored.pyc").exists()
    assert not (copied_package / "ignored.pth").exists()
    assert not (project / ".venv" / "Scripts" / "pymupdf.exe").exists()
    assert not (project / ".venv" / "Scripts" / "helper.exe").exists()
    assert not (copied_package / "IMPORTED").exists()
    assert project_python.inspect_project_python(project)["available"] is True


def test_failed_copy_keeps_incomplete_marker_and_inspection_fails_closed(tmp_path, monkeypatch):
    project = tmp_path / "project"
    site_packages = tmp_path / "host" / "site-packages"
    project.mkdir()
    site_packages.mkdir(parents=True)
    _fake_pymupdf(site_packages)
    _use_host_site_packages(monkeypatch, site_packages)

    def fail_copy(*_args, **_kwargs):
        raise OSError("simulated interrupted package copy")

    monkeypatch.setattr(project_python, "_copy_file", fail_copy)
    result = project_python.prepare_project_python(project, packages=("pymupdf",))
    marker = project / ".venv" / project_python._PREPARING_MARKER
    assert result["ok"] is False and result["created"] is True
    assert marker.is_file()
    inspected = project_python.inspect_project_python(project)
    assert inspected["available"] is False and inspected["status"] == "invalid"

    marker_before = marker.read_bytes()
    retry = project_python.prepare_project_python(project)
    assert retry["ok"] is False and retry["error"]["code"] == "environment_exists"
    assert marker.read_bytes() == marker_before


@pytest.mark.parametrize("alias", ["PYMUPDF/__init__.py", "pymupdf/__init__.py.", "pymupdf/__init__.py "])
def test_windows_record_alias_fails_before_environment_creation(tmp_path, monkeypatch, alias):
    project, site_packages = tmp_path / "project", tmp_path / "host"
    project.mkdir()
    site_packages.mkdir()
    _fake_pymupdf(site_packages, extra_files=[alias])
    _use_host_site_packages(monkeypatch, site_packages)
    key = project_python._destination_key
    monkeypatch.setattr(project_python, "_destination_key", lambda parts: key(parts, windows=True))
    result = project_python.prepare_project_python(project, packages=("pymupdf",))
    assert not result["ok"] and result["error"]["code"] == "package_metadata_invalid"
    assert not (project / ".venv").exists()


def test_windows_cross_distribution_alias_fails_before_environment_creation(tmp_path, monkeypatch):
    project, site_packages = tmp_path / "project", tmp_path / "host"
    project.mkdir()
    site_packages.mkdir()
    for name, alias in [("first", "shared.py"), ("second", "SHARED.py")]:
        _, info = _fake_distribution(site_packages, name, "1.0", module=name)
        (site_packages / alias).write_text("VALUE = 1", encoding="utf-8")
        with (info / "RECORD").open("a", encoding="utf-8") as stream:
            stream.write(alias + ",,\n")
    _use_host_site_packages(monkeypatch, site_packages)
    key = project_python._destination_key
    monkeypatch.setattr(project_python, "_destination_key", lambda parts: key(parts, windows=True))
    result = project_python.prepare_project_python(project, packages=("first", "second"))
    assert not result["ok"] and result["error"]["code"] == "package_file_conflict"
    assert not (project / ".venv").exists()
