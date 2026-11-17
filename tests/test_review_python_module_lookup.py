"""Executable facts for module commands do not claim module execution or trust."""
import os
from pathlib import Path

import pytest

from mewcode.permissions.action_review import ActionReviewBinding, execution_environment
from mewcode.permissions.command_sources import host_python_lookup, python_script_arguments
from mewcode.permissions.reviewer import SafetyReviewer
from tests.test_auto_action_lifecycle import setup_agent


@pytest.mark.skipif(os.name != 'nt', reason='Windows NoProfile child PATH only')
@pytest.mark.parametrize('command', [
    'python -m unittest scripts.test_synthetic -v',
    'python -m unittest discover -s scripts -p test_synthetic.py',
    'python -m json.tool report.json',
])
def test_module_command_has_bound_executable_facts_without_running_or_reading_module(tmp_path, monkeypatch, command):
    executable = tmp_path / '.venv' / 'Scripts' / 'python.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'synthetic executable identity, never executed')
    config = tmp_path / '.venv' / 'pyvenv.cfg'
    config.write_text('home = C:/synthetic-runtime\n', encoding='utf-8')
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'test_synthetic.py').write_text('raise RuntimeError("MUST NOT IMPORT")\n', encoding='utf-8')
    def never_launch(*args, **kwargs):
        pytest.fail('prospective lookup must not launch a process')
    monkeypatch.setattr('subprocess.Popen', never_launch)
    agent, _, _ = setup_agent(tmp_path)
    monkeypatch.setattr(agent, '_uses_native_commands', lambda arguments=None: False)
    arguments = {'command': command}
    binding = ActionReviewBinding.capture(agent, 'Bash', arguments)
    lookup = binding.environment['host_python_lookup']
    assert lookup['prospective_executable']['path'] == str(executable.resolve())
    assert lookup['execution_confirmed'] is False
    assert lookup['phase'] == 'prospective_lookup'
    assert python_script_arguments(command) is None
    facts = SafetyReviewer(None, str(tmp_path))._readonly_action_facts('Bash', arguments)
    assert 'program_source' not in facts.get('action_target', {})
    assert binding.is_current(agent, 'Bash', arguments)
    config.write_text('home = C:/changed-runtime\n', encoding='utf-8')
    assert not binding.is_current(agent, 'Bash', arguments)
    binding = ActionReviewBinding.capture(agent, 'Bash', arguments)
    executable.write_bytes(b'changed synthetic executable identity')
    assert not binding.is_current(agent, 'Bash', arguments)
    monkeypatch.setattr(agent, '_uses_native_commands', lambda arguments=None: True)
    assert 'host_python_lookup' not in execution_environment(agent, 'Bash', arguments)


@pytest.mark.parametrize('command', [
    'python -m unittest; echo second',
    'python -m unittest > output.txt',
    'echo python -m unittest',
    'python -m',
    'python -m ../unittest',
    'python -c "print(1)"',
])
def test_lookup_stays_unknown_for_unsupported_or_composed_commands(tmp_path, command):
    assert host_python_lookup(command, tmp_path) is None
