"""Read-only evidence for a planned Python script; never an execution verdict."""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import re
import shutil

SCRIPT_SOURCE_MAX_BYTES = 64 * 1024
LOCAL_IMPORT_MAX_FILES = 8
LOCAL_IMPORT_MAX_BYTES = 128 * 1024
LOCAL_IMPORT_MAX_NAMES = 64
ARGUMENT_SOURCE_MAX_FILES = 8
ARGUMENT_SOURCE_MAX_BYTES = 128 * 1024
ARGUMENT_SOURCE_MAX_ARGS = 64


def _python_arguments(command):
    from mewcode.permissions.command_rules import parse_command
    if not isinstance(command, str):
        return None
    parsed = parse_command(command, 'powershell' if os.name == 'nt' else 'bash')
    if not parsed.safe_for_prefix_rules or len(parsed.segments) != 1 or parsed.segments[0].redirections:
        return None
    argv = parsed.segments[0].arguments
    if len(argv) < 2:
        return None
    name = argv[0].replace('\\', '/').rsplit('/', 1)[-1]
    if not re.fullmatch(r'python(?:[23](?:\.\d+)?)?(?:\.exe)?', name, re.I):
        return None
    return argv


def python_script_arguments(command):
    argv = _python_arguments(command)
    return argv if argv and argv[1].lower().endswith('.py') else None


def _python_lookup_arguments(command):
    """Recognize executable lookup independently of script-source collection.

    A literal module name does not resolve or import that module. Module code,
    dependencies and effects remain unknown to this prospective lookup.
    """
    argv = _python_arguments(command)
    if argv and (argv[1].lower().endswith('.py') or
                 (len(argv) >= 3 and argv[1] == '-m' and
                  all(part.isidentifier() for part in argv[2].split('.')))):
        return argv
    return None


def script_source(data):
    value = {'source': 'host_current_script_bytes', 'grants_authority': False,
             'complete': False, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
             'scope': 'Program text only; dependencies and runtime effects still require review.'}
    if len(data) > SCRIPT_SOURCE_MAX_BYTES:
        return {**value, 'omission_reason': 'script_source_exceeds_64KiB'}
    encoding = 'utf-8-sig' if data.startswith(b'\xef\xbb\xbf') else 'utf-8'
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        return {**value, 'omission_reason': 'script_source_not_utf8'}
    return {**value, 'complete': True, 'encoding': encoding, 'text': text}


def local_import_sources(path, workspace, source):
    """Read bounded sibling module candidates, never import or resolve packages.

    These are prospective source facts, not Python's runtime import resolution.
    Transitive, package, installed and dynamic imports remain uninspected.
    The caller includes every fact in the ordinary action/cache binding.
    """
    value = {'source': 'host_current_sibling_import_bytes', 'grants_authority': False,
             'runtime_resolution_confirmed': False, 'dependency_graph_complete': False,
             'scope': 'Direct syntactic sibling .py candidates only; package, installed, '
                      'transitive and dynamic dependencies and runtime effects remain unknown.',
             'files': [], 'unresolved_imports': [], 'omissions': []}
    if not source.get('complete'):
        value['omissions'].append({'reason': 'entry_source_unavailable'})
        return value
    try:
        tree = ast.parse(source['text'])
    except (SyntaxError, ValueError, RecursionError):
        value['omissions'].append({'reason': 'entry_source_not_parseable'})
        return value
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add('.' * node.level + (node.module or ''))
    ordered = sorted(names)
    if len(ordered) > LOCAL_IMPORT_MAX_NAMES:
        value['omissions'].append({'reason': 'local_import_name_limit',
                                  'omitted_count': len(ordered) - LOCAL_IMPORT_MAX_NAMES})
    used = 0
    for name in ordered[:LOCAL_IMPORT_MAX_NAMES]:
        if not name.isidentifier():
            value['unresolved_imports'].append({'module': name, 'reason': 'package_or_relative_import_uninspected'})
            continue
        try:
            candidate = (path.parent / (name + '.py')).resolve()
            if not candidate.is_relative_to(workspace):
                value['omissions'].append({'module': name, 'reason': 'local_import_outside_workspace'})
                continue
            if not candidate.is_file():
                value['unresolved_imports'].append({'module': name, 'reason': 'no_sibling_py_source'})
                continue
            if len(value['files']) >= LOCAL_IMPORT_MAX_FILES:
                value['omissions'].append({'module': name, 'reason': 'local_import_file_limit'})
                continue
            size = candidate.stat().st_size
            if size > SCRIPT_SOURCE_MAX_BYTES:
                observed = {'complete': False, 'grants_authority': False, 'bytes': size,
                            'omission_reason': 'script_source_exceeds_64KiB'}
            elif used + size > LOCAL_IMPORT_MAX_BYTES:
                observed = {'complete': False, 'grants_authority': False, 'bytes': size,
                            'omission_reason': 'local_import_total_bytes_limit'}
            else:
                # A bounded read also handles growth between stat and read.
                with candidate.open('rb') as handle:
                    data = handle.read(min(SCRIPT_SOURCE_MAX_BYTES, LOCAL_IMPORT_MAX_BYTES - used) + 1)
                if used + len(data) > LOCAL_IMPORT_MAX_BYTES:
                    observed = {'complete': False, 'grants_authority': False,
                                'omission_reason': 'local_import_total_bytes_limit'}
                elif len(data) > SCRIPT_SOURCE_MAX_BYTES:
                    observed = {'complete': False, 'grants_authority': False,
                                'omission_reason': 'script_source_exceeds_64KiB'}
                else:
                    observed = script_source(data)
                used += len(data)
            value['files'].append({'module': name, 'path': str(candidate), 'program_source': observed})
            if not observed['complete']:
                value['omissions'].append({'module': name, 'reason': observed['omission_reason']})
        except (OSError, ValueError, RuntimeError):
            value['omissions'].append({'module': name, 'reason': 'local_import_source_unavailable'})
    return value


def argument_sources(argv, workspace, entry_path):
    """Read literal .py argument candidates without interpreting the program.

    A file named in argv can be input, output or dynamically loaded code. These
    facts do not claim which role it has, resolve imports or authorize execution.
    No expansion, package discovery or recursive dependency walk is performed.
    """
    value = {'source': 'host_current_literal_py_argument_bytes', 'grants_authority': False,
             'runtime_resolution_confirmed': False, 'dependency_graph_complete': False,
             'scope': 'Literal workspace .py arguments only; their runtime role and '
                      'all further dependencies remain unverified.',
             'files': [], 'omissions': []}
    if len(argv) - 2 > ARGUMENT_SOURCE_MAX_ARGS:
        value['omissions'].append({'reason': 'argument_source_argument_limit',
                                  'omitted_count': len(argv) - 2 - ARGUMENT_SOURCE_MAX_ARGS})
    seen = {entry_path}
    used = 0
    for index, argument in enumerate(argv[2:2 + ARGUMENT_SOURCE_MAX_ARGS], 2):
        # Accept --client=path.py as well as a separate literal path token.
        raw = argument.split('=', 1)[1] if argument.startswith('--') and '=' in argument else argument
        if raw.startswith('-') or not raw.lower().endswith('.py'):
            continue
        try:
            requested = Path(raw)
            path = (requested if requested.is_absolute() else workspace / requested).resolve()
            if not path.is_relative_to(workspace):
                value['omissions'].append({'argument_index': index, 'reason': 'argument_source_outside_workspace'})
                continue
            if path in seen:
                continue
            seen.add(path)
            if not path.is_file():
                value['omissions'].append({'argument_index': index, 'reason': 'argument_source_unavailable'})
                continue
            if len(value['files']) >= ARGUMENT_SOURCE_MAX_FILES:
                value['omissions'].append({'argument_index': index, 'reason': 'argument_source_file_limit'})
                continue
            size = path.stat().st_size
            reason = ('script_source_exceeds_64KiB' if size > SCRIPT_SOURCE_MAX_BYTES else
                      'argument_source_total_bytes_limit' if used + size > ARGUMENT_SOURCE_MAX_BYTES else None)
            if reason:
                source = {'complete': False, 'grants_authority': False, 'bytes': size, 'omission_reason': reason}
            else:
                with path.open('rb') as handle:
                    data = handle.read(min(SCRIPT_SOURCE_MAX_BYTES, ARGUMENT_SOURCE_MAX_BYTES - used) + 1)
                reason = ('argument_source_total_bytes_limit' if used + len(data) > ARGUMENT_SOURCE_MAX_BYTES else
                          'script_source_exceeds_64KiB' if len(data) > SCRIPT_SOURCE_MAX_BYTES else None)
                source = ({'complete': False, 'grants_authority': False, 'omission_reason': reason}
                          if reason else script_source(data))
                used += len(data)
            value['files'].append({'argument_index': index, 'path': str(path), 'program_source': source})
            if not source['complete']:
                value['omissions'].append({'argument_index': index, 'reason': source['omission_reason']})
        except (OSError, ValueError, RuntimeError):
            value['omissions'].append({'argument_index': index, 'reason': 'argument_source_unavailable'})
    return value


def _file_identity(path, max_bytes):
    result = {'path': str(path), 'exists': path.is_file()}
    if not result['exists']:
        return result
    stat = path.stat()
    result.update(bytes=stat.st_size, modified_ns=stat.st_mtime_ns)
    if stat.st_size <= max_bytes:
        result['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def host_python_lookup(command, work_dir):
    """Mirror Windows Bash's child PATH lookup, without launching any process.

    POSIX login shells can rewrite PATH, so no resolution claim is made there.
    The action binding rechecks these facts before ordinary dispatch.
    """
    argv = _python_lookup_arguments(command)
    if os.name != 'nt' or argv is None:
        return None
    from mewcode.tools.bash import _child_env
    root = Path(work_dir).resolve()
    env = _child_env(str(root))
    requested = argv[0]
    if '/' in requested or '\\' in requested:
        candidate = Path(requested)
        executable = str((candidate if candidate.is_absolute() else root / candidate).resolve())
    else:
        executable = shutil.which(requested, path=env.get('PATH', ''))
    value = {'phase': 'prospective_lookup', 'execution_confirmed': False,
             'requested_executable': requested,
             'method': 'Windows NoProfile shell using Bash._child_env PATH',
             'path': env.get('PATH', ''), 'pathext': env.get('PATHEXT', ''),
             'python_environment': {key: env[key] for key in (
                 'PYTHONPATH', 'PYTHONHOME', 'PYTHONNOUSERSITE', 'PYTHONSAFEPATH',
                 'PYTHONDONTWRITEBYTECODE') if key in env},
             'scope': 'Prospective executable lookup only; not an execution receipt or a sandbox claim.'}
    try:
        value['prospective_executable'] = _file_identity(Path(executable).resolve(), 16 * 1024 * 1024) if executable else None
        config = root / '.venv' / 'pyvenv.cfg'
        value['project_venv_config'] = _file_identity(config, SCRIPT_SOURCE_MAX_BYTES)
    except (OSError, ValueError, RuntimeError):
        value['lookup_incomplete'] = True
    return value
