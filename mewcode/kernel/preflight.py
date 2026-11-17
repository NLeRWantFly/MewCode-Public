"""Read-only environment checks for declared capabilities. Never repairs."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys


def interpreter_gap(workspace, interpreter):
    root = Path(workspace)
    requested = Path(interpreter)
    if not requested.is_absolute():
        requested = root / requested
    if interpreter.lower() in {'python', 'python3'}:
        venv = root / '.venv'
        requested = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python') if venv.exists() else Path(
            shutil.which(interpreter) or root / interpreter)
    if not requested.is_file():
        return '声明的解释器不存在：' + str(requested)
    environment = requested.parent.parent
    config = environment / 'pyvenv.cfg'
    if config.is_file():
        if config.stat().st_size > 16384:
            return 'pyvenv.cfg 超出预检读取上限。'
        fields = dict(line.split('=', 1) for line in config.read_text(encoding='utf-8-sig').splitlines() if '=' in line)
        fields = {key.strip().lower(): value.strip() for key, value in fields.items()}
        base = fields.get('executable') or fields.get('base-executable')
        if not base:
            base = str(Path(fields.get('home') or '') / ('python.exe' if os.name == 'nt' else 'python'))
        if not Path(base).is_file():
            return 'venv 找不到基础解释器：' + base
    return ''


def powershell_quotes():
    if os.name != 'nt':
        return True, '当前宿主不使用 PowerShell。'
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if not shell:
        return False, 'PowerShell 不可用。'
    # Fixed host code only; never execute a workspace script during preflight.
    executable = str(sys.executable).replace("'", "''")
    command = f"& '{executable}' -c 'import json,sys;print(json.dumps(sys.argv[1:]))' 'value=\"quoted\"'"
    try:
        result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, timeout=5, creationflags=0x08000000)
        passed = result.returncode == 0 and json.loads(result.stdout.strip()) == ['value="quoted"']
        return passed, 'PowerShell 内嵌引号保留。' if passed else 'PowerShell 外部进程传参未保留内嵌引号。'
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False, 'PowerShell 引号探针未得到有效回执。'


def network_reachable(host):
    try:
        with socket.create_connection((host, 443), timeout=1):
            return True, '网络目标可建立连接：' + host
    except OSError as exc:
        return False, '网络目标不可达：' + host + '（' + type(exc).__name__ + '）'


def inspect(manifest, *, probe_network=False, native_probe=None, quote_probe=None, network_probe=None,
            command_backend="native"):
    if command_backend not in {"native", "host_reviewed"}:
        raise ValueError("Unknown command backend")
    host_execution = command_backend == "host_reviewed"
    findings = []
    nodes = manifest.get('nodes') or {}
    commands = [key for key, scope in nodes.items() if scope and scope['command_execution']]
    undeclared = [key for key, scope in nodes.items() if scope is None]
    # An approved manifest with undeclared nodes is not startable: otherwise the
    # first scoped tool call is what discovers it, and every such call is refused.
    fatal = bool(manifest.get('approved')) and bool(undeclared)

    def add(code, passed, message, affected, *, warning=False, status=None):
        findings.append({'code': code, 'status': status or ('passed' if passed else 'missing'),
            'severity': 'warning' if warning else ('info' if passed else 'error'),
            'entry_ids': sorted(affected), 'message': message})
    if commands:
        if host_execution:
            add('reviewed_host_executor', True, 'Auto 命令按现有权限审查后在本机执行。', commands)
        else:
            if native_probe is None:
                from mewcode.sandbox.native_gate import native_gate_status
                native_probe = native_gate_status
            try:
                status = native_probe()
                add('isolated_executor', status.ready, status.reason, commands)
            except (OSError, ValueError) as exc:
                add('isolated_executor', False, str(exc), commands)
        passed, reason = (quote_probe or powershell_quotes)()
        add('powershell_quotes', passed, reason, commands, warning=host_execution and not passed)
    interpreters, hosts = {}, {}
    for key, scope in nodes.items():
        if scope is None:
            add('undeclared_capabilities', False,
                '能力范围尚未声明，本次运行不能启动；先确认该节点可用的工具、路径与外部服务。'
                if fatal else '能力范围尚未声明；能力调用时需要确认。',
                [key], warning=not fatal, status='missing' if fatal else 'declared')
            continue
        for name in scope['interpreters']:
            interpreters.setdefault(name, []).append(key)
        for host in scope['network_hosts']:
            hosts.setdefault(host, []).append(key)
    for name, affected in interpreters.items():
        try:
            gap = interpreter_gap(manifest['workspace'], name)
        except (OSError, UnicodeError, ValueError) as exc:
            gap = '解释器配置无法核验：' + type(exc).__name__
        add('base_interpreter', not gap, gap or '解释器与基础解释器路径存在：' + name, affected,
            warning=host_execution and bool(gap))
    if probe_network:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for host, (passed, reason) in zip(hosts, pool.map(network_probe or network_reachable, hosts), strict=True):
                add('network_scope', passed, reason, hosts[host], warning=host_execution and not passed)
    else:
        for host, affected in hosts.items():
            add('network_scope', False, '确认计划后检查网络连通性：' + host, affected, warning=True)
    return findings


def inspect_node(manifest, node_id, **probes):
    """Check only the node about to dispatch; missing scope still fails closed."""
    selected = {**manifest, 'nodes': {node_id: (manifest.get('nodes') or {}).get(node_id)}}
    return inspect(selected, **probes)
