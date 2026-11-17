"""Executor-only candidate tests. Test code is execution, never read-only audit."""
import asyncio
import json
from typing import Literal
from pydantic import BaseModel, Field
from mewcode.tools.base import Tool, ToolResult
from mewcode.experts.candidate_verification import verify_candidate


class VerifyCandidateParams(BaseModel):
    runner: Literal["pytest", "unittest", "npm_test"]
    targets: list[str] = Field(default_factory=list, max_length=20, description="Candidate-relative test files/directories; pytest ::selectors allowed. No shell commands or options.")
    timeout: int = Field(default=120, ge=1, le=600)


class VerifyCandidate(Tool):
    name = "VerifyCandidate"
    description = ("Run targeted tests inside the candidate using a detached dependency copy, returning actual cwd, exit code and baseline/candidate diff. "
                   "No .git or dependency links to the original are installed. This executes project test code and is subject to command approval; it is not a read-only tool or OS sandbox.")
    category = "command"
    params_model = VerifyCandidateParams

    def __init__(self, context):
        self.context = dict(context)

    async def execute(self, params):
        try:
            result = await verify_candidate(self.context, params.runner, params.targets, params.timeout)
        except asyncio.TimeoutError:
            return ToolResult(output="候选验证超时；依赖复制已停止并已请求清理本次进程树，不能计为测试通过。", is_error=True)
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"候选验证环境不可用：{exc}。请说明缺失条件并换方法，不要重复原命令。", is_error=True)
        return ToolResult(output=json.dumps(result, ensure_ascii=False),
                          is_error=result["exit_code"] != 0 or not result["source_unchanged"])
