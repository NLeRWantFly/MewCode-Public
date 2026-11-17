"""HTTP target checks, including redirects and the address actually dialled."""
from __future__ import annotations

import asyncio
import ipaddress
import inspect
import socket
from types import MethodType

import httpx
import httpcore
from httpx._utils import URLPattern, get_environment_proxies

from mewcode.permissions.network_policy import NetworkDenied, NetworkPolicy, active_policy


def _address_key(value: str) -> str:
    """Canonicalize IP spellings for alias comparisons without changing dial data."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return value
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.compressed
    return address.compressed


async def checked_addresses(
    host: str, port: int, *, policy: NetworkPolicy | None = None,
) -> list[str]:
    """Resolve and reject addresses matching any configured blacklist entry.

    The optional policy pins one immutable policy revision across the DNS work.
    If omitted, the policy active in the current task scope is used.
    """
    if policy is None:
        policy = active_policy()
    if policy is not None:
        policy.check_host(host)
    loop = asyncio.get_running_loop()
    targets = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(row[4][0] for row in targets))
    if policy is None or not policy.denied_domains:
        return addresses

    denied: dict[str, str] = {}
    for entry in policy.denied_domains:
        domain = entry.removeprefix('*.')
        try:
            ipaddress.ip_address(domain)
            denied[_address_key(domain)] = entry
        except ValueError:
            try:
                rows = await loop.getaddrinfo(domain, port, type=socket.SOCK_STREAM)
            except socket.gaierror:
                continue
            denied.update({_address_key(row[4][0]): entry for row in rows})
    for address in addresses:
        if _address_key(address) in denied:
            raise NetworkDenied(
                f'NETWORK_BLACKLISTED: {host} 的目标地址命中网络黑名单 '
                f'{denied[_address_key(address)]}。'
            )
    return addresses


class _PolicyBackend:
    def __init__(self, backend):
        self.backend = backend

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        values = await checked_addresses(
            host.decode() if isinstance(host, bytes) else host, port
        )
        last_error = None
        for address in values:
            # httpcore keeps the origin hostname for TLS SNI. Dial the validated
            # address directly so a second DNS lookup cannot change the target.
            try:
                return await self.backend.connect_tcp(address, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options)
            except (OSError, httpx.TransportError, httpcore.ConnectError) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise NetworkDenied('NETWORK_NO_ADDRESS: 网络目标未解析出可用地址。')

    async def connect_unix_socket(self, *args, **kwargs):
        raise NetworkDenied('NETWORK_INVALID_TARGET: 任务 HTTP 请求不能连接本地 Unix socket。')

    async def sleep(self, seconds):
        return await self.backend.sleep(seconds)


def _proxy_api():
    """Load the narrowly supported httpcore proxy internals lazily.

    Direct transports do not depend on private proxy APIs. If an installed
    httpcore changes this shape, a proxy pool fails closed during creation.
    """
    try:
        from httpcore import AsyncHTTPConnection, AsyncHTTPProxy
        from httpcore._async.http_proxy import AsyncForwardHTTPConnection, AsyncTunnelHTTPConnection
        from httpcore._models import Origin as CoreOrigin, Request as CoreRequest, URL as CoreURL
    except (ImportError, AttributeError) as exc:
        raise NetworkDenied('NETWORK_PROXY_UNSUPPORTED: 代理连接接口不受支持。') from exc
    return (AsyncHTTPProxy, AsyncForwardHTTPConnection, AsyncTunnelHTTPConnection,
            AsyncHTTPConnection, CoreRequest, CoreURL, CoreOrigin)


def _proxy_unsupported(cause: BaseException | None = None) -> NetworkDenied:
    error = NetworkDenied('NETWORK_PROXY_UNSUPPORTED: 代理连接接口不受支持。')
    if cause is not None:
        error.__cause__ = cause
    return error


def _create_pinned_proxy_connection(pool, origin):
    """Per-pool create_connection hook; the direct pool is never modified."""
    try:
        creator, supported = pool._mewcode_proxy_pin_api
        connection = creator(pool, origin)
        (_, forward_type, tunnel_type, core_connection_type, core_request, core_url,
         core_origin) = supported
        if type(origin) is not core_origin:
            raise TypeError('unexpected proxy origin implementation')
        if type(connection) is tunnel_type:
            mode = 'connect'
        elif type(connection) is forward_type:
            mode = 'forward'
        else:
            raise TypeError('unexpected proxy connection implementation')
        if getattr(connection, '_remote_origin', None) != origin:
            raise TypeError('proxy connection does not match its pool origin')
        inner = getattr(connection, '_connection', None)
        if type(inner) is not core_connection_type or not callable(getattr(inner, 'handle_async_request', None)):
            raise TypeError('unexpected proxy inner connection implementation')
        connection._connection = _PinnedProxyConnection(
            inner, mode, core_request, core_url, origin
        )
        return connection
    except NetworkDenied:
        raise
    except Exception as exc:
        raise _proxy_unsupported(exc) from exc


def _install_proxy_pinning(pool) -> None:
    try:
        (pool_type, forward_type, tunnel_type, core_connection_type, core_request,
         core_url, core_origin) = _proxy_api()
        creator = getattr(pool, 'create_connection', None)
        if (type(pool) is not pool_type or not inspect.ismethod(creator)
                or creator.__self__ is not pool or creator.__func__ is not pool_type.create_connection
                or not all(hasattr(pool, name) for name in ('_network_backend', '_proxy_url', '_keepalive_expiry'))):
            raise TypeError('unexpected AsyncHTTPProxy pool shape')
        supported = (pool_type, forward_type, tunnel_type, core_connection_type,
                     core_request, core_url, core_origin)
        pool._mewcode_proxy_pin_api = (pool_type.create_connection, supported)
        pool.create_connection = MethodType(_create_pinned_proxy_connection, pool)
    except NetworkDenied:
        raise
    except Exception as exc:
        raise _proxy_unsupported(exc) from exc


def _valid_proxy_host(host: str) -> str:
    # Use the task policy's canonical hostname validation. Convert IPv4-mapped
    # IPv6 literals to IPv4 so blacklist comparisons and proxy authorities use
    # the same address identity.
    from mewcode.permissions.network_policy import normalize_host
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ':' in host:
            raise NetworkDenied('NETWORK_INVALID_TARGET: 代理目标 authority 无效。')
        try:
            return normalize_host(host)
        except ValueError as exc:
            raise NetworkDenied('NETWORK_INVALID_TARGET: 代理目标 authority 无效。') from exc
    return _canonical_proxy_ip(address.compressed)


def _canonical_proxy_ip(value: str) -> str:
    """Normalize proxy IPs, mapping IPv4-compatible authorities to IPv4."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise NetworkDenied('NETWORK_INVALID_TARGET: 代理目标 IP 地址无效。') from exc
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            raise NetworkDenied('NETWORK_INVALID_TARGET: 不支持带 scope-id 的 IPv6 代理目标。')
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
    return address.compressed


def _parse_connect_authority(target: bytes) -> tuple[str, int]:
    if not isinstance(target, bytes) or not target or len(target) > 4096:
        raise NetworkDenied('NETWORK_INVALID_TARGET: CONNECT authority 无效。')
    try:
        text = target.decode('ascii')
    except UnicodeDecodeError as exc:
        raise NetworkDenied('NETWORK_INVALID_TARGET: CONNECT authority 无效。') from exc
    if text.startswith('['):
        close = text.find(']')
        if close < 0 or text[close + 1:close + 2] != ':':
            raise NetworkDenied('NETWORK_INVALID_TARGET: CONNECT authority 无效。')
        host, port_text = text[1:close], text[close + 2:]
        try:
            parsed_host = ipaddress.ip_address(host)
        except ValueError as exc:
            raise NetworkDenied('NETWORK_INVALID_TARGET: CONNECT authority 无效。') from exc
        if not isinstance(parsed_host, ipaddress.IPv6Address):
            raise NetworkDenied('NETWORK_INVALID_TARGET: CONNECT authority 无效。')
    else:
        host, separator, port_text = text.rpartition(':')
        if not separator or ':' in host:
            raise NetworkDenied('NETWORK_INVALID_TARGET: CONNECT authority 无效。')
    if (not host or not port_text.isascii() or not port_text.isdigit() or len(port_text) > 5):
        raise NetworkDenied('NETWORK_INVALID_TARGET: CONNECT authority 无效。')
    try:
        port = int(port_text)
    except ValueError as exc:
        raise NetworkDenied('NETWORK_INVALID_TARGET: CONNECT authority 无效。') from exc
    if not 1 <= port <= 65535:
        raise NetworkDenied('NETWORK_INVALID_TARGET: CONNECT authority 无效。')
    return _valid_proxy_host(host), port


def _ip_authority(address: str, port: int) -> bytes:
    return f'{_ip_host(address)}:{port}'.encode('ascii')


def _ip_host(address: str) -> str:
    parsed = ipaddress.ip_address(_canonical_proxy_ip(address))
    if isinstance(parsed, ipaddress.IPv6Address):
        return f'[{parsed.compressed}]'
    return parsed.compressed


def _origin_fields(origin, mode: str) -> tuple[bytes, str, str, int]:
    try:
        scheme = origin.scheme
        raw_host = origin.host.decode('ascii')
        port = origin.port
    except (AttributeError, UnicodeError, TypeError) as exc:
        raise NetworkDenied('NETWORK_PROXY_UNSUPPORTED: 代理 origin 格式不受支持。') from exc
    expected_scheme = b'https' if mode == 'connect' else b'http'
    if (scheme != expected_scheme or not isinstance(port, int) or isinstance(port, bool)
            or not 1 <= port <= 65535):
        raise NetworkDenied('NETWORK_PROXY_UNSUPPORTED: 代理 origin 格式不受支持。')
    host = _valid_proxy_host(raw_host)
    return scheme, raw_host, host, port


def _origin_host_forms(raw_host: str, normalized_host: str) -> set[bytes]:
    """Return equivalent host spellings for validating httpcore's old target."""
    forms = {raw_host.encode('ascii'), normalized_host.encode('ascii')}
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        return forms
    if isinstance(address, ipaddress.IPv6Address):
        compressed = address.compressed.encode('ascii')
        forms.add(compressed)
        forms.add(b'[' + raw_host.encode('ascii') + b']')
        forms.add(b'[' + compressed + b']')
    return forms


def _expected_connect_targets(raw_host: str, normalized_host: str, port: int) -> set[bytes]:
    suffix = b':' + str(port).encode('ascii')
    return {host + suffix for host in _origin_host_forms(raw_host, normalized_host)}


def _forward_origin_path(
    target: bytes, raw_host: str, normalized_host: str, port: int,
) -> bytes:
    """Validate the absolute URL's original authority and return its path/query."""
    if not isinstance(target, bytes) or not target or len(target) > 1024 * 1024:
        raise NetworkDenied('NETWORK_INVALID_TARGET: HTTP 代理目标 URL 无效。')
    port_forms = [b''] if port != 80 else [b'', b':80']
    if port != 80:
        port_forms = [b':' + str(port).encode('ascii')]
    for host in _origin_host_forms(raw_host, normalized_host):
        for port_suffix in port_forms:
            prefix = b'http://' + host + port_suffix
            if not target.startswith(prefix):
                continue
            path = target[len(prefix):]
            if path and path[:1] not in (b'/', b'?'):
                continue
            if b'#' in path:
                continue
            if not path or path.startswith(b'?'):
                return b'/' + path
            return path
    raise NetworkDenied('NETWORK_INVALID_TARGET: HTTP 代理目标 origin 与已选连接不匹配。')


async def _pinned_address(host: str, port: int) -> str:
    values = await checked_addresses(host, port)
    if not values:
        raise NetworkDenied('NETWORK_NO_ADDRESS: 网络目标未解析出可用地址。')
    canonical = []
    for value in values:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise NetworkDenied('NETWORK_INVALID_TARGET: DNS 返回了无效代理地址。') from exc
        canonical.append(_canonical_proxy_ip(address.compressed))
    return canonical[0]


def _replace_header(headers, name: bytes, value: bytes):
    output = []
    inserted = False
    for key, old_value in headers:
        if not isinstance(key, bytes) or not isinstance(old_value, bytes):
            raise NetworkDenied('NETWORK_PROXY_UNSUPPORTED: 代理请求头格式不受支持。')
        if key.lower() == name.lower():
            if not inserted:
                output.append((name, value))
                inserted = True
        else:
            output.append((key, old_value))
    if not inserted:
        output.append((name, value))
    return output


class _PinnedProxyConnection:
    """Intercept only the proxy's CONNECT/forward request to the origin."""

    def __init__(self, connection, mode: str, request_type, url_type, origin):
        self._connection = connection
        self._mode = mode
        self._request_type = request_type
        self._url_type = url_type
        _, self._origin_host_raw, self._origin_host, self._origin_port = _origin_fields(
            origin, mode
        )

    def __getattr__(self, name):
        return getattr(self._connection, name)

    async def handle_async_request(self, request):
        if type(request.url) is not self._url_type:
            raise _proxy_unsupported()
        if self._mode == 'connect':
            if request.method != b'CONNECT':
                raise _proxy_unsupported()
            expected = _expected_connect_targets(
                self._origin_host_raw, self._origin_host, self._origin_port
            )
            if request.url.target not in expected:
                raise NetworkDenied('NETWORK_INVALID_TARGET: CONNECT origin 与已选连接不匹配。')
            address = await _pinned_address(self._origin_host, self._origin_port)
            authority = _ip_authority(address, self._origin_port)
            url = self._url_type(scheme=request.url.scheme, host=request.url.host,
                                 port=request.url.port, target=authority)
            headers = _replace_header(request.headers, b'Host', authority)
        elif self._mode == 'forward':
            path = _forward_origin_path(
                request.url.target, self._origin_host_raw, self._origin_host,
                self._origin_port,
            )
            address = await _pinned_address(self._origin_host, self._origin_port)
            port_suffix = b'' if self._origin_port == 80 else (
                b':' + str(self._origin_port).encode('ascii')
            )
            pinned_target = (
                b'http://' + _ip_host(address).encode('ascii') + port_suffix + path
            )
            url = self._url_type(scheme=request.url.scheme, host=request.url.host,
                                 port=request.url.port, target=pinned_target)
            # Keep the original Host and Proxy-Authorization headers intact.
            headers = request.headers
        else:
            raise _proxy_unsupported()
        pinned_request = self._request_type(method=request.method, url=url, headers=headers,
            content=request.stream, extensions=request.extensions)
        return await self._connection.handle_async_request(pinned_request)


class _TransportGeneration:
    """A set of direct/proxy pools belonging to one policy revision."""

    def __init__(self, revision: str | None, make_transport):
        self.revision = revision
        self.direct = make_transport()
        self.mounts = sorted(
            (
                URLPattern(pattern),
                None if proxy is None else make_transport(proxy),
            )
            for pattern, proxy in get_environment_proxies().items()
        )
        self.active_requests = 0
        self.retired = False
        self.closed = False

    def for_request(self, request: httpx.Request) -> httpx.AsyncBaseTransport:
        for pattern, transport in self.mounts:
            if pattern.matches(request.url):
                # A NO_PROXY match maps to None and falls through to direct.
                return transport or self.direct
        return self.direct

    async def aclose(self):
        transports = [self.direct]
        transports.extend(transport for _, transport in self.mounts if transport is not None)
        await asyncio.gather(*(transport.aclose() for transport in transports))


class _LeasedStream(httpx.AsyncByteStream):
    """Keep an old generation alive while its response body is still in use."""

    def __init__(self, stream: httpx.AsyncByteStream, release):
        self._stream = stream
        self._release = release
        self._released = False

    async def __aiter__(self):
        try:
            async for chunk in self._stream:
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self):
        if self._released:
            return
        self._released = True
        try:
            await self._stream.aclose()
        finally:
            await self._release()


class PolicyTransport(httpx.AsyncBaseTransport):
    """HTTP transport that retains env proxies and checks every scoped target.

    HTTPX follows redirects above the transport, so each redirect re-enters
    ``handle_async_request`` and receives the same URL and resolved-address
    checks before either a direct or proxy connection is opened.
    """

    def __init__(self, verify=True):
        self._lock = asyncio.Lock()
        self._generations: set[_TransportGeneration] = set()
        self._closed = False
        self._verify = verify
        self._generation = self._new_generation(None)
        self._generations.add(self._generation)

    @staticmethod
    def _make_transport(proxy: str | None = None, *, verify=True) -> httpx.AsyncHTTPTransport:
        transport = httpx.AsyncHTTPTransport(trust_env=False, proxy=proxy, verify=verify)
        pool = transport._pool
        pool._network_backend = _PolicyBackend(pool._network_backend)
        if proxy is not None:
            _install_proxy_pinning(pool)
        return transport

    def _new_generation(self, revision: str | None) -> _TransportGeneration:
        return _TransportGeneration(
            revision, lambda proxy=None: self._make_transport(proxy, verify=self._verify)
        )

    async def _acquire_generation(self, revision: str | None) -> _TransportGeneration:
        close_after: _TransportGeneration | None = None
        async with self._lock:
            if self._closed:
                raise RuntimeError('PolicyTransport is closed')
            if self._generation.revision != revision:
                previous = self._generation
                previous.retired = True
                self._generation = self._new_generation(revision)
                self._generations.add(self._generation)
                if previous.active_requests == 0 and not previous.closed:
                    previous.closed = True
                    self._generations.discard(previous)
                    close_after = previous
            generation = self._generation
            generation.active_requests += 1
        if close_after is not None:
            await close_after.aclose()
        return generation

    async def _release_generation(self, generation: _TransportGeneration) -> None:
        close_after = None
        async with self._lock:
            generation.active_requests -= 1
            if generation.retired and generation.active_requests == 0 and not generation.closed:
                generation.closed = True
                self._generations.discard(generation)
                close_after = generation
        if close_after is not None:
            await close_after.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        policy = active_policy()
        if policy is not None:
            # Validate the HTTP destination independently of the selected proxy.
            # Proxies receive origin URLs and do not pass the origin through our
            # connect_tcp hook, so this preflight is required for proxy requests.
            policy.check_url(str(request.url))
            await checked_addresses(
                request.url.host,
                request.url.port or (443 if request.url.scheme == 'https' else 80),
                policy=policy,
            )
        generation = await self._acquire_generation(
            policy.revision if policy is not None else None
        )
        try:
            response = await generation.for_request(request).handle_async_request(request)
        except BaseException:
            await self._release_generation(generation)
            raise
        response.stream = _LeasedStream(
            response.stream, lambda: self._release_generation(generation)
        )
        return response

    async def aclose(self):
        close_now = []
        async with self._lock:
            self._closed = True
            for generation in tuple(self._generations):
                generation.retired = True
                if generation.active_requests == 0 and not generation.closed:
                    generation.closed = True
                    self._generations.discard(generation)
                    close_now.append(generation)
        await asyncio.gather(*(generation.aclose() for generation in close_now))
