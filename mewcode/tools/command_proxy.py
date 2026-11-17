"""An explicit, observed proxy route for already-approved ordinary host commands.

The proxy restricts its own HTTPS tunnels; it is not process isolation. Native
and container requirements are never replaced by this transport. No request
bodies, application credentials or transient proxy credentials enter receipts.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
from urllib.parse import urlsplit
import uuid

from mewcode.permissions.network_policy import NetworkDenied, normalize_host, policy_for

MAX_CONNECTIONS = 32
MAX_TUNNEL_BYTES = 128 * 1024 * 1024


def proxy_origins(targets):
    if not isinstance(targets, (list, tuple)) or not 1 <= len(targets) <= 16:
        raise ValueError('network=proxy requires 1–16 exact HTTPS origins')
    result = {}
    for target in targets:
        if not isinstance(target, str) or any(ord(c) < 33 for c in target) or '\\' in target:
            raise ValueError('Invalid HTTPS proxy origin')
        value = urlsplit(target)
        if (value.scheme != 'https' or not value.hostname or value.username is not None
                or value.password is not None or value.path not in {'', '/'} or value.query or value.fragment):
            raise ValueError('Proxy targets must be HTTPS origins without credentials, paths or queries')
        host, port = normalize_host(value.hostname), value.port if value.port is not None else 443
        if not 1 <= port <= 65535:
            raise ValueError('Invalid HTTPS proxy port')
        authority = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
        result[(host, port)] = 'https://' + authority
    return result


class CommandProxy:
    def __init__(self, owner, targets):
        self.owner, self.targets = owner, proxy_origins(targets)
        self.policy = policy_for(owner)
        for host, _ in self.targets:
            self.policy.check_host(host)
        self.authorization = getattr(owner, 'task_authorization', None)
        self.task_id = getattr(owner, 'task_id', '')
        self._check_current()
        self.identity = uuid.uuid4().hex
        self.password = secrets.token_hex(32)
        self.auth = b'Basic ' + base64.b64encode(('mewcode:' + self.password).encode())
        self.server = None
        self.watcher = None
        self.tasks, self.writers = set(), set()
        self.closed, self.revoked, self.untrusted = False, False, 0
        self.rows = []

    def _check_current(self):
        if policy_for(self.owner).revision != self.policy.revision:
            raise NetworkDenied('Network policy changed during the command')
        if getattr(self.owner, 'task_id', '') != self.task_id:
            raise NetworkDenied('Command task identity changed')
        authorization = getattr(self.owner, 'task_authorization', None)
        if authorization != self.authorization:
            raise NetworkDenied('Task authorization changed during the command')
        if authorization is not None:
            if time.time() >= authorization.expires_at or authorization.task_id != self.task_id:
                raise NetworkDenied('Current task authorization is unavailable')
            hosts = {normalize_host(host) for host in authorization.network_hosts}
            if any(host not in hosts for host, _ in self.targets):
                raise NetworkDenied('Proxy target is outside the current task network scope')

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._accept, '127.0.0.1', 0, limit=8192)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f'http://mewcode:{self.password}@127.0.0.1:{port}'
        self.watcher = asyncio.create_task(self._watch())
        return self

    async def __aexit__(self, *_):
        self.closed = True
        if self.watcher is not None:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
        self.server.close()
        await self.server.wait_closed()
        for writer in tuple(self.writers):
            writer.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch(self):
        while True:
            await asyncio.sleep(.2)
            try:
                self._check_current()
            except (OSError, ValueError):
                self.revoked = True
                for writer in tuple(self.writers):
                    writer.close()
                return

    def _accept(self, reader, writer):
        if self.closed or self.revoked or len(self.tasks) >= 16:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _handle(self, reader, writer):
        self.writers.add(writer)
        upstream = None
        row = None
        connected = False
        pumps = []
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            lines = raw.decode('ascii').split('\r\n')
            headers = {}
            for line in lines[1:-2]:
                key, value = line.split(':', 1)
                key = key.lower().strip()
                if key in headers:
                    raise ValueError('Duplicate proxy header')
                headers[key] = value.strip().encode('ascii')
            if not hmac.compare_digest(headers.get('proxy-authorization', b''), self.auth):
                self.untrusted += 1
                writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\n'
                    b'Proxy-Authenticate: Basic realm="MewCode"\r\nConnection: close\r\n\r\n')
                await writer.drain()
                return
            if len(self.rows) >= MAX_CONNECTIONS:
                raise NetworkDenied('Command proxy connection budget exhausted')
            row = {'id': len(self.rows) + 1, 'status': 'denied', 'sent_bytes': 0, 'received_bytes': 0}
            self.rows.append(row)
            method, authority, version = lines[0].split(' ')
            if method != 'CONNECT' or version not in {'HTTP/1.0', 'HTTP/1.1'}:
                raise NetworkDenied('Only HTTPS CONNECT is supported')
            origins = proxy_origins(['https://' + authority])
            target = next(iter(origins))
            if target not in self.targets:
                raise NetworkDenied('Target is outside this command proxy scope')
            host, port = target
            row['target'] = self.targets[target]
            self._check_current()
            if self.closed or self.revoked:
                raise NetworkDenied('Command proxy is closed')
            from mewcode.tools.network_transport import checked_addresses
            addresses = await asyncio.wait_for(checked_addresses(host, port, policy=self.policy), 10)
            self._check_current()
            # One checked address, one connection attempt; never replay an upload.
            if not addresses:
                raise NetworkDenied('Proxy target has no address')
            up_reader, upstream = await asyncio.wait_for(asyncio.open_connection(addresses[0], port), 10)
            self.writers.add(upstream)
            self._check_current()
            row.update(status='connected', remote_address=addresses[0])
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            await writer.drain()
            connected = True

            async def transfer(source, destination, field):
                while chunk := await source.read(65536):
                    self._check_current()
                    if self.revoked or self.closed or row[field] + len(chunk) > MAX_TUNNEL_BYTES:
                        raise NetworkDenied('Proxy tunnel closed or byte budget exhausted')
                    destination.write(chunk)
                    await destination.drain()
                    row[field] += len(chunk)
            pumps = [asyncio.create_task(transfer(reader, upstream, 'sent_bytes')),
                     asyncio.create_task(transfer(up_reader, writer, 'received_bytes'))]
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            row['status'] = 'closed'
        except asyncio.CancelledError:
            if row is not None and connected:
                row['status'] = 'closed_at_command_end'
            raise
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
            if row is not None:
                # TLS clients can reset the TCP tunnel after reading a complete
                # response. The proxy cannot infer application failure from a
                # peer close; retain it and let the child report its result.
                state = ('closed_by_peer' if connected and isinstance(error, (ConnectionResetError, BrokenPipeError))
                         else 'interrupted' if connected else 'denied')
                row.update(status=state, error_type=type(error).__name__)
                if isinstance(error, NetworkDenied):
                    row['reason'] = str(error)
            if not connected:
                try:
                    writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
                    await writer.drain()
                except OSError:
                    pass
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for item in (writer, upstream):
                if item is not None:
                    self.writers.discard(item)
                    item.close()

    def environment(self):
        return {**{name: self.url for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')},
                'NO_PROXY': '', 'no_proxy': '', 'MEWCODE_COMMAND_PROXY_ID': self.identity}

    def evidence(self):
        return {'schema': 'mewcode-command-proxy-v1', 'id': self.identity,
                'route': 'host_injected_proxy', 'policy_revision': self.policy.revision,
                'allowed_origins': sorted(self.targets.values()), 'connections': self.rows,
                'policy_revoked': self.revoked, 'unauthenticated_connections': self.untrusted,
                'denied_connections': sum(row['status'] == 'denied' for row in self.rows),
                'interrupted_connections': sum(row['status'] == 'interrupted' for row in self.rows),
                'direct_socket_enforcement': False,
                'evidence_scope': 'Host-observed CONNECT tunnels and byte counts; not TLS bodies or OCR acceptance.'}


async def run_proxied_command(command, *, work_dir, timeout, targets):
    from mewcode.permissions.network_policy import active_policy_owner
    from mewcode.permissions.modes import PermissionMode
    from mewcode.tools.bash import _child_env, _shell_command, _terminate_process_tree
    from mewcode.tools.base import ToolResult
    from mewcode.experts.command_receipts import execution_observation
    owner = active_policy_owner()
    try:
        if (owner is None or owner.mode not in {PermissionMode.AUTO, PermissionMode.BYPASS}
                or owner.uses_native_commands({'network': 'proxy', 'publication': 'publish'})):
            raise NetworkDenied('Recorded proxy requires the approved ordinary host command backend')
        proxy = CommandProxy(owner, targets)
    except (OSError, ValueError) as error:
        return ToolResult(str(error), is_error=True, host_observation={'command_execution':
            execution_observation('denied', command_started=False, reason=str(error))})
    proc, stdout, stderr = None, b'', b''
    status, reason = 'spawn_failed', ''
    try:
        async with proxy:
            env = {**_child_env(work_dir), **proxy.environment()}
            shell, arguments = _shell_command(command)
            proc = await asyncio.create_subprocess_exec(shell, *arguments, cwd=work_dir, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != 'nt',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
            status = 'exited'
    except TimeoutError:
        status, reason = 'timed_out', 'Proxied command timed out; no automatic retry'
        if proc is not None:
            await _terminate_process_tree(proc)
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            await _terminate_process_tree(proc)
        raise
    except (OSError, ValueError) as error:
        reason = type(error).__name__
    def sanitized(value):
        return (value.decode(errors='replace').replace(proxy.password, '[redacted]')
                .replace(proxy.auth.split(b' ', 1)[1].decode('ascii'), '[redacted]'))
    out, err = sanitized(stdout), sanitized(stderr)
    observation = execution_observation(status, stdout=out, stderr=err, reason=reason,
        exit_code=proc.returncode if proc is not None else None, command_started=proc is not None)
    observation['transport'] = proxy.evidence()
    observation['command_sha256'] = hashlib.sha256(command.encode()).hexdigest()
    # A listening proxy may receive rejected probes or requests the child
    # handles itself. Preserve every refusal without changing a successful
    # process result into failure (which could trigger duplicate paid work).
    failed = status != 'exited' or proc.returncode != 0 or proxy.revoked
    output = f'STDOUT:\n{out}\nSTDERR:\n{err}\nEXIT CODE: {observation.get("exit_code")}\n'
    output += 'HOST PROXY RECEIPT: ' + json.dumps(proxy.evidence(), ensure_ascii=False)
    if reason:
        output += '\n' + reason
    return ToolResult(output, is_error=failed, host_observation={'command_execution': observation})
