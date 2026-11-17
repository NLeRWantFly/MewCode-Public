from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from mewcode.agent import Agent
from mewcode.tools import ToolRegistry
from mewcode.tools.base import Tool, ToolCallComplete, ToolResult, active_work_dir
from mewcode.tools.bash import Bash, Params as BashParams
from mewcode.tools.read_file import Params as ReadParams
from mewcode.tools.read_file import ReadFile
from mewcode.tools.write_file import Params as WriteParams
from mewcode.tools.write_file import WriteFile


class _NoParams(BaseModel):
    pass


class _WorkDirProbe(Tool):
    name = "WorkDirProbe"
    description = "Return the active tool working directory."
    params_model = _NoParams

    async def execute(self, params: _NoParams) -> ToolResult:
        return ToolResult(output=active_work_dir() or "")


@pytest.mark.asyncio
async def test_tool_context_is_isolated_between_parallel_agents(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "value.txt").write_text("left", encoding="utf-8")
    (right / "value.txt").write_text("right", encoding="utf-8")
    tool = ReadFile()

    left_result, right_result = await asyncio.gather(
        tool.run(ReadParams(file_path="value.txt"), work_dir=str(left)),
        tool.run(ReadParams(file_path="value.txt"), work_dir=str(right)),
    )

    assert left_result.output == "1\tleft"
    assert right_result.output == "1\tright"


@pytest.mark.asyncio
async def test_relative_write_stays_inside_agent_workdir(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = await WriteFile().run(
        WriteParams(file_path="nested/result.txt", content="ok"),
        work_dir=str(project),
    )

    assert not result.is_error
    assert (project / "nested" / "result.txt").read_text(encoding="utf-8") == "ok"
    assert not (tmp_path / "nested" / "result.txt").exists()


@pytest.mark.asyncio
async def test_agent_passes_its_workdir_to_tools(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_WorkDirProbe())
    agent = Agent(object(), registry, "anthropic", work_dir=str(tmp_path))

    result = await agent._execute_single_tool_direct(
        ToolCallComplete("probe", "WorkDirProbe", {})
    )

    assert result.result.output == str(tmp_path)


@pytest.mark.asyncio
async def test_bash_reports_cwd_and_native_exit_code(tmp_path: Path) -> None:
    python = Path(sys.executable).name
    cwd_result = await Bash().run(
        BashParams(command=f'{python} -c "import os; print(os.getcwd())"'),
        work_dir=str(tmp_path),
    )
    failure = await Bash().run(
        BashParams(command=f'{python} -c "import sys; sys.exit(7)"'),
        work_dir=str(tmp_path),
    )

    assert not cwd_result.is_error
    assert str(tmp_path) in cwd_result.output
    assert "EXIT CODE: 0" in cwd_result.output
    assert failure.is_error
    assert "EXIT CODE: 7" in failure.output
