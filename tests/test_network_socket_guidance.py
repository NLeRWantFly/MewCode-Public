from dataclasses import replace

import pytest

from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.tools.command_sandbox import network_socket_hint, run_native_command


def failed_socket():
    return NativeExecutionReceipt('exited', backend='mewcode-windows-srt', command_started=True, exit_code=1,
        stdout='URLError: [WinError 10013]', cleanup_confirmed=True,
        transaction_id='synthetic-failed-upload', publication_state='not_published',
        network_policy={'enabled': True, 'preflight': {'ready': True}})


@pytest.mark.asyncio
async def test_live_proxy_socket_failure_keeps_effect_unknown_and_never_retries(monkeypatch):
    from unittest.mock import AsyncMock
    execute = AsyncMock(return_value=failed_socket())
    monkeypatch.setattr('mewcode.tools.command_sandbox.execute_native_command', execute)
    result = await run_native_command('synthetic', work_dir='.', timeout=15)
    execute.assert_awaited_once()
    assert result.is_error
    assert 'ProxyHandler({})' in result.output
    assert 'ProxyHandler()' in result.output
    assert '不能断定外部请求完全未发送' in result.output
    assert result.host_observation['network_diagnostic']['automatic_retry'] is False
    assert result.host_observation['command_execution']['execution_condition']['kind'] == 'effect_unknown'


@pytest.mark.parametrize('changes', [
    {'network_policy': None}, {'network_policy': {'enabled': False}},
    {'network_policy': {'enabled': True, 'preflight': {'ready': False}}},
    {'status': 'denied', 'command_started': False, 'exit_code': None}, {'stdout': 'Connection timed out'},
    {'exit_code': 0, 'publication_state': 'discarded'},
])
def test_other_failures_are_not_misdiagnosed_as_proxy_configuration(changes):
    assert network_socket_hint(replace(failed_socket(), **changes)) == ''
