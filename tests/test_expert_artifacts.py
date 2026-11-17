from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.experts import (
    ArtifactError,
    ArtifactNotExecutable,
    ArtifactValidationError,
    ExpertArtifactWorkspace,
)
from mewcode.experts.artifacts import (
    SKILL_FILENAME,
    STATE_APPROVED,
    STATE_DRAFT,
    STATE_REJECTED,
    STATE_VALIDATED,
)

VALID_SKILL = """---
name: helper
description: A helper skill for tests.
---

Do the thing.
"""

NO_NAME_SKILL = """---
description: missing the name field.
---

Body.
"""

NAME_MISMATCH_SKILL = """---
name: other-name
description: name does not match the directory.
---

Body.
"""


@pytest.fixture
def ctx():
    """Per-test scratch area under the gitignored .artifacts_scratch root.

    Created with Path.mkdir's default permissive mode so it also works under
    sandboxes that deny writes inside 0o700-created directories.
    """
    base = Path(".artifacts_scratch") / "expert-artifacts" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    yield SimpleNamespace(
        base=base,
        workspace=ExpertArtifactWorkspace.for_run(base, "run-1"),
    )
    shutil.rmtree(base, ignore_errors=True)


def test_happy_path_draft_to_executable(ctx) -> None:
    artifact = ctx.workspace.write_skill("helper", VALID_SKILL)
    assert artifact.state == STATE_DRAFT
    assert not artifact.is_executable

    artifact.validate()
    assert artifact.state == STATE_VALIDATED
    assert not artifact.is_executable

    with pytest.raises(ArtifactNotExecutable):
        artifact.require_executable()

    artifact.approve("executor")
    assert artifact.state == STATE_APPROVED
    assert artifact.is_executable
    artifact.require_executable()  # no raise


def test_validation_failure_blocks_everything(ctx) -> None:
    artifact = ctx.workspace.write_skill("broken", NO_NAME_SKILL)
    with pytest.raises(ArtifactValidationError):
        artifact.validate()
    assert artifact.state == STATE_REJECTED
    with pytest.raises(ArtifactNotExecutable):
        artifact.require_executable()
    with pytest.raises(ArtifactError):
        artifact.approve("executor")


def test_frontmatter_name_must_match_directory(ctx) -> None:
    artifact = ctx.workspace.write_skill("helper", NAME_MISMATCH_SKILL)
    with pytest.raises(ArtifactValidationError):
        artifact.validate()
    assert artifact.state == STATE_REJECTED


def test_approve_requires_validation(ctx) -> None:
    artifact = ctx.workspace.write_skill("helper", VALID_SKILL)
    with pytest.raises(ArtifactError, match="未通过校验"):
        artifact.approve("executor")


def test_request_approval_requires_validation(ctx) -> None:
    artifact = ctx.workspace.write_skill("helper", VALID_SKILL)
    with pytest.raises(ArtifactError, match="未通过校验"):
        artifact.request_approval("planner")


def test_execution_requires_approval_message(ctx) -> None:
    artifact = ctx.workspace.write_skill("helper", VALID_SKILL)
    with pytest.raises(ArtifactNotExecutable, match="未经批准，不得执行"):
        artifact.require_executable()


@pytest.mark.parametrize(
    "bad",
    ["Bad Name", "-lead", "a/b", "..", "a..b", ""],
)
def test_invalid_names_are_rejected(ctx, bad) -> None:
    with pytest.raises(ArtifactError, match="非法 skill 名字"):
        ctx.workspace.write_skill(bad, VALID_SKILL)


def test_artifacts_land_in_run_scoped_dir(ctx) -> None:
    ctx.workspace.write_skill("helper", VALID_SKILL)
    expected = (
        ctx.base / "experts" / "run-1" / "artifacts" / "helper" / SKILL_FILENAME
    )
    assert expected.is_file()
    assert ctx.workspace.list_artifacts() == ["helper"]


def test_remove_and_recycle(ctx) -> None:
    workspace = ctx.workspace
    workspace.write_skill("helper", VALID_SKILL)
    workspace.remove_artifact("helper")
    assert workspace.list_artifacts() == []
    workspace.write_skill("helper", VALID_SKILL)
    workspace.recycle()
    assert not workspace.root.exists()


def test_load_skill_reads_existing_artifact(ctx) -> None:
    ctx.workspace.write_skill("helper", VALID_SKILL)
    artifact = ctx.workspace.load_skill("helper")
    assert artifact.state == STATE_DRAFT
    definition = artifact.validate()
    assert definition.name == "helper"


def test_load_missing_skill_raises(ctx) -> None:
    with pytest.raises(ArtifactError, match="不存在"):
        ctx.workspace.load_skill("ghost")


def test_verify_within_sandbox_passes_inside_root(ctx) -> None:
    from mewcode.permissions import PathSandbox

    sandbox = PathSandbox(str(ctx.base))
    ctx.workspace.verify_within_sandbox(sandbox)  # no raise


def test_verify_within_sandbox_rejects_outside_root(ctx) -> None:
    from mewcode.permissions import PathSandbox

    sandbox = PathSandbox(str(ctx.base))
    outside = ExpertArtifactWorkspace(ctx.base.parent / "outside-artifacts")
    with pytest.raises(ArtifactError, match="越出沙箱"):
        outside.verify_within_sandbox(sandbox)
