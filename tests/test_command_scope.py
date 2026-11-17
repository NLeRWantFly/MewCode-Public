from pydantic import BaseModel
import pytest

from mewcode.experts.command_scope import wrap_allowlisted_command
from mewcode.tools.base import Tool, ToolResult


class Params(BaseModel):
    command: str


class FakeBash(Tool):
    name = "Bash"
    description = "fake"
    params_model = Params
    category = "command"

    def __init__(self):
        self.commands = []

    async def execute(self, params):
        self.commands.append(params.command)
        return ToolResult(output="ok")


@pytest.mark.asyncio
async def test_delegated_command_guard_requires_exact_allowlist_match():
    inner = FakeBash()
    guarded = wrap_allowlisted_command(inner, ("python --version",))

    rejected = await guarded.execute(Params(command="python --version; pip list"))
    accepted = await guarded.execute(Params(command="  python --version  "))

    assert rejected.is_error is True
    assert accepted.is_error is False
    assert inner.commands == ["  python --version  "]


@pytest.mark.asyncio
async def test_repeated_probe_reuses_output_without_another_execution():
    inner = FakeBash()
    guarded = wrap_allowlisted_command(inner, ("synthetic",))
    first = await guarded.execute(Params(command="synthetic"))
    cached = await guarded.execute(Params(command="synthetic"))
    assert inner.commands == ["synthetic"]
    assert cached.output.startswith('COMMAND_RECEIPT_REUSED:')
    assert cached.output.endswith(first.output) and cached.host_observation["command_reused"]


@pytest.mark.asyncio
async def test_concurrent_probe_is_not_dispatched_twice_and_cancelled_probe_stays_unknown():
    import asyncio
    started = asyncio.Event()
    release = asyncio.Event()

    class PendingBash(FakeBash):
        async def execute(self, params):
            self.commands.append(params.command)
            started.set()
            await release.wait()
            return ToolResult("done")

    inner = PendingBash()
    guarded = wrap_allowlisted_command(inner, ("synthetic",))
    task = asyncio.create_task(guarded.execute(Params(command="synthetic")))
    await started.wait()
    pending = await guarded.execute(Params(command="synthetic"))
    assert pending.is_error and pending.host_observation["command_execution"]["status"] == "outcome_unknown"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancelled = await guarded.execute(Params(command="synthetic"))
    assert cancelled.is_error and inner.commands == ["synthetic"]


@pytest.mark.asyncio
@pytest.mark.parametrize('status,started,exit_code', [('exited', True, 0), ('timed_out', True, None)])
async def test_helper_cached_observation_keeps_origin_without_false_fresh_execution(status, started, exit_code):
    from mewcode.experts.command_receipts import execution_observation, compact_execution_observation
    class ObservedBash(FakeBash):
        async def execute(self, params):
            self.commands.append(params.command)
            return ToolResult('old stdout', is_error=status != 'exited', host_observation={
                'command_execution':execution_observation(status,command_started=started,exit_code=exit_code)})
    inner = ObservedBash()
    guarded = wrap_allowlisted_command(inner, ('synthetic',), receipt_context={'dispatch_id':'original-dispatch'})
    first = await guarded.execute(Params(command='synthetic'))
    cached = await guarded.execute(Params(command='synthetic'))
    assert inner.commands == ['synthetic']
    assert first.output == 'old stdout'
    assert first.host_observation['command_execution']['status'] == status
    assert cached.output.startswith('COMMAND_RECEIPT_REUSED:')
    assert cached.output.endswith(first.output)
    assert cached.host_observation['reused'] is True
    compact = compact_execution_observation(cached.host_observation)
    source = compact['command_receipt_reuse']
    assert source['dispatch_id'] == 'original-dispatch'
    assert source['command_execution']['status'] == status
    if status == 'exited':
        assert compact['command_execution'] == {'status':'not_started','command_started':False,'exit_code':None}
    else:
        assert compact['command_execution']['status'] == 'outcome_unknown'
        assert compact['command_execution']['command_started'] is None
        assert cached.is_error is True
