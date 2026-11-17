"""Host-owned network switch and deny list, shared by all task descendants."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import stat
import threading
from urllib.parse import urlsplit
import weakref


class NetworkDenied(ValueError):
    pass


def normalize_host(value: str) -> str:
    raw = value.strip().lower().rstrip('.')
    if raw.startswith('[') and raw.endswith(']'):
        raw = raw[1:-1]
    try:
        address = ipaddress.ip_address(raw)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            return address.ipv4_mapped.compressed
        return address.compressed
    except ValueError:
        pass
    if not raw or any(c in raw for c in '/\\:@?#\x00') or any(ord(c) < 33 for c in raw):
        raise ValueError('请输入域名或 IP 地址，不包含协议、路径或端口')
    result = raw.encode('idna').decode('ascii')
    if len(result) > 253 or any(not part or len(part) > 63 or part.startswith('-')
                             or part.endswith('-') or not all(c.isalnum() or c == '-' for c in part)
                             for part in result.split('.')):
        raise ValueError('网络黑名单中的域名无效')
    return result


def normalize_entry(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('网络黑名单必须是字符串数组')
    raw = value.strip()
    wildcard = raw.startswith('*.')
    host = normalize_host(raw[2:] if wildcard else raw)
    if wildcard:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return '*.' + host
        raise ValueError('IP 地址不能使用子域名通配符')
    return host


@dataclass(frozen=True)
class NetworkPolicy:
    enabled: bool = True
    denied_domains: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict) -> NetworkPolicy:
        if not isinstance(data, dict) or type(data.get('enabled', True)) is not bool:
            raise ValueError('网络访问开关必须是布尔值')
        values = data.get('denied_domains', [])
        if not isinstance(values, list):
            raise ValueError('网络黑名单必须是数组')
        return cls(data.get('enabled', True), tuple(sorted({normalize_entry(v) for v in values})))

    @property
    def revision(self) -> str:
        return hashlib.sha256(json.dumps([self.enabled, self.denied_domains],
                                        ensure_ascii=True).encode()).hexdigest()[:20]

    def as_dict(self) -> dict:
        return {'enabled': self.enabled, 'denied_domains': list(self.denied_domains),
                'revision': self.revision}

    def denied_entry(self, host: str) -> str:
        host = normalize_host(host)
        for entry in self.denied_domains:
            base = normalize_host(entry.removeprefix('*.'))
            if host == base or host.endswith('.' + base):
                return entry
        return ''

    def check_host(self, host: str) -> str:
        if not self.enabled:
            raise NetworkDenied('NETWORK_DISABLED: 任务联网已关闭，请在沙箱设置中开启网络访问。')
        normalized = normalize_host(host)
        entry = self.denied_entry(normalized)
        if entry:
            raise NetworkDenied(f'NETWORK_BLACKLISTED: {normalized} 命中网络黑名单 {entry}。')
        return normalized

    def check_url(self, url: str) -> str:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise NetworkDenied('NETWORK_INVALID_TARGET: 网络目标无效。') from exc
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise NetworkDenied('NETWORK_INVALID_TARGET: 需要不含登录信息的 HTTP(S) 地址。')
        if port is not None and not 1 <= port <= 65535:
            raise NetworkDenied('NETWORK_INVALID_TARGET: 网络端口无效。')
        return self.check_host(parsed.hostname)


class NetworkPolicyStore:
    def __init__(self, path: Path | None = None):
        if path is None:
            from mewcode.runtime_paths import user_data_root
            path = user_data_root() / 'sandbox-network.json'
        self.path = Path(path)
        self._lock = _thread_lock_for(self.path)

    def read(self) -> NetworkPolicy:
        with self._lock:
            try:
                return NetworkPolicy.from_dict(json.loads(self.path.read_text(encoding='utf-8')))
            except FileNotFoundError:
                return NetworkPolicy()
            except (OSError, ValueError, TypeError) as exc:
                raise NetworkDenied('NETWORK_POLICY_INVALID: 沙箱联网配置无法读取，请修正配置。') from exc

    def save(self, payload: dict, *, expected_revision: str) -> NetworkPolicy:
        policy = NetworkPolicy.from_dict(payload)
        with _exclusive_policy_lock(self.path, self._lock):
            if self.read().revision != expected_revision:
                raise ValueError('网络设置已变化，请刷新后保存')
            temporary = self.path.with_name(self.path.name + '.' + os.urandom(8).hex() + '.tmp')
            try:
                temporary.write_text(json.dumps(policy.as_dict(), ensure_ascii=False), encoding='utf-8')
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return policy


_STORE_LOCK_REGISTRY_GUARD = threading.Lock()
_STORE_THREAD_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()


def _thread_lock_for(path: Path) -> threading.RLock:
    """Share a local lock between instances for the same policy file."""
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _STORE_LOCK_REGISTRY_GUARD:
        lock = _STORE_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _STORE_THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _exclusive_policy_lock(path: Path, thread_lock: threading.RLock):
    """Serialize policy compare-and-swap across instances, threads and processes."""
    lock_path = Path(str(path) + '.lock')
    with thread_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_BINARY', 0)
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(lock_path, flags, 0o600)
        handle = None
        locked = False
        try:
            # A byte range is required by msvcrt.locking.  flock works on an
            # empty file too, but a persistent byte keeps one portable format.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'\0')
            if os.name == 'posix':
                # Restrict only this user-data lock file; do not alter its parent.
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            handle = os.fdopen(fd, 'r+b', closefd=True)
            fd = -1
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if handle is not None:
                if locked:
                    try:
                        if os.name == 'nt':
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                handle.close()
            if fd >= 0:
                os.close(fd)


_ACTIVE: ContextVar[object | None] = ContextVar('mewcode_task_network_policy', default=None)


def policy_for(owner=None) -> NetworkPolicy:
    value = getattr(owner, 'network_policy', None)
    if isinstance(value, NetworkPolicy):
        return value
    store = getattr(owner, 'network_policy_store', None)
    return (store or NetworkPolicyStore()).read()


def active_policy() -> NetworkPolicy | None:
    owner = _ACTIVE.get()
    return policy_for(owner) if owner is not None else None


def active_policy_owner():
    """Current task checker, also used to report the selected command backend."""
    return _ACTIVE.get()


@contextmanager
def task_network_scope(owner):
    token = _ACTIVE.set(owner)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
