from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
COMPAT_DIR = ROOT / "native" / "windows" / "git-compat"
HEADER = COMPAT_DIR / "mewcode-appcontainer.h"
PATCH = COMPAT_DIR / "git-v2.55.0.windows.3.patch"


def _hunks(text):
    lines = text.splitlines()
    paths = []
    hunks = []
    index = 0

    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
            assert match, line
            assert match.group(1) == match.group(2), line
            paths.append(match.group(1))
            index += 1
            continue

        match = re.fullmatch(
            r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)",
            line,
        )
        if not match:
            index += 1
            continue

        old_count = int(match.group(2) or "1")
        new_count = int(match.group(4) or "1")
        seen_old = 0
        seen_new = 0
        index += 1
        while (
            index < len(lines)
            and (seen_old < old_count or seen_new < new_count)
        ):
            body_line = lines[index]
            if body_line.startswith("\\ No newline at end of file"):
                index += 1
                continue
            assert body_line.startswith((" ", "+", "-")), body_line
            if body_line[0] in " -":
                seen_old += 1
            if body_line[0] in " +":
                seen_new += 1
            index += 1

        assert (seen_old, seen_new) == (old_count, new_count), line
        hunks.append((old_count, new_count))

    return paths, hunks


def test_patch_is_well_formed_and_scoped():
    patch_text = PATCH.read_text(encoding="ascii")
    paths, hunks = _hunks(patch_text)

    assert paths == [
        "compat/mewcode-appcontainer.h",
        "setup.c",
        "compat/mingw.c",
    ]
    assert hunks
    assert "52ca1113d651127f89477a8763f86ab20f645e1d" in patch_text
    assert "v2.55.0.windows.3" in PATCH.name


def test_added_git_header_matches_shared_header():
    patch_text = PATCH.read_text(encoding="ascii")
    lines = patch_text.splitlines()
    marker = "+++ b/compat/mewcode-appcontainer.h"
    start = lines.index(marker) + 1
    added = []

    for line in lines[start:]:
        if line.startswith("diff --git "):
            break
        if line.startswith("+"):
            added.append(line[1:])

    assert "\n".join(added) + "\n" == HEADER.read_text(encoding="ascii")


def test_helper_keeps_identity_and_volume_checks_mandatory():
    source = HEADER.read_text(encoding="ascii")

    for required in (
        "mewcode_ac_is_appcontainer",
        "MEWCODE_TOKEN_IS_APPCONTAINER",
        "mewcode_ac_stdio_handles_are_valid",
        "if (result || flags != 0)",
        "original_error != ERROR_ACCESS_DENIED",
        "VOLUME_NAME_NT",
        "VOLUME_NAME_NONE",
        "CompareStringOrdinal",
        "mewcode_ac_same_file(handle, reopened)",
        "dwVolumeSerialNumber",
        "nFileIndexHigh",
        "nFileIndexLow",
        "FILE_SHARE_READ | FILE_SHARE_WRITE",
        "FILE_FLAG_BACKUP_SEMANTICS",
        "ERROR_INSUFFICIENT_BUFFER",
    ):
        assert required in source

    assert "GLOBALROOT" not in source
    assert source.count("GetFinalPathNameByHandleW") == 2


def test_mingw_patch_replaces_exactly_four_calls():
    patch_text = PATCH.read_text(encoding="ascii")

    assert patch_text.count("mingw_get_final_path_by_handle(") == 5
    assert "#ifdef MEWCODE_APPCONTAINER_COMPAT" in patch_text
    assert "#else\n+\treturn GetFinalPathNameByHandleW" in patch_text


def test_c_header_lines_are_ascii_and_at_most_80_columns():
    source = HEADER.read_bytes()
    assert all(byte < 128 for byte in source)
    for line in source.decode("ascii").splitlines():
        assert len(line.expandtabs(8)) <= 80, line
