import http from 'node:http';
import https from 'node:https';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { lookup as dnsLookup } from 'node:dns/promises';
import { connect as netConnect, isIP } from 'node:net';
import { connect as tlsConnect } from 'node:tls';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const EMPTY_PROXY = Object.freeze({ http: '', https: '', noProxy: '' });
const LPAC_PROXY_PORTS = new Set([60080, 60081]);

async function defaultResolve(host) {
  if (isIP(host)) return [host];
  const rows = await dnsLookup(host, { all: true, verbatim: true });
  return rows.map(row => row.address);
}

function recordsToAddresses(records, canonicalizeHost) {
  const rows = Array.isArray(records) ? records : [records];
  const addresses = [];
  for (const row of rows) {
    const raw = typeof row === 'string' ? row : row?.address;
    const normalized = typeof raw === 'string' ? canonicalizeHost(raw) : undefined;
    if (!normalized || !isIP(normalized)) throw new Error('DNS resolver returned a non-IP address');
    if (!addresses.includes(normalized)) addresses.push(normalized);
  }
  if (addresses.length === 0) throw new Error('DNS resolver returned no addresses');
  return addresses;
}

function defaultPort(protocol) {
  return protocol === 'https:' ? 443 : 80;
}

function response(res, status, text = '', extraHeaders = {}) {
  const body = Buffer.from(text, 'utf8');
  res.writeHead(status, {
    connection: 'close',
    'content-length': String(body.length),
    'content-type': 'text/plain; charset=utf-8',
    ...extraHeaders,
  });
  res.end(body);
}

function connectResponse(socket, status, message) {
  if (socket.destroyed) return;
  const body = Buffer.from(message, 'utf8');
  const reason = status === 400 ? 'Bad Request' : status === 403 ? 'Forbidden'
    : status === 407 ? 'Proxy Authentication Required'
    : status === 503 ? 'Service Unavailable' : 'Bad Gateway';
  const authentication = status === 407
    ? 'Proxy-Authenticate: Basic realm="SRT parent proxy shim"\r\n' : '';
  socket.end(`HTTP/1.1 ${status} ${reason}\r\n${authentication}Connection: close\r\nContent-Length: ${body.length}\r\n\r\n${body}`);
}

function protectedEndpoint(url, parentHelpers) {
  const host = parentHelpers.canonicalizeHost(parentHelpers.stripBrackets(url.hostname));
  const port = Number(url.port) || (url.protocol === 'https:' ? 443 : 80);
  return { host, port };
}

function sameEndpoint(left, right) {
  return left.host === right.host && left.port === right.port;
}

function parseConnectTarget(raw, parentHelpers) {
  if (typeof raw !== 'string' || raw.length === 0 || /[\r\n\0]/.test(raw)) return undefined;
  let url;
  try {
    url = new URL(`http://${raw}`);
  } catch {
    return undefined;
  }
  if (url.username || url.password || url.pathname !== '/' || url.search || url.hash) return undefined;
  const host = parentHelpers.canonicalizeHost(parentHelpers.stripBrackets(url.hostname));
  const port = url.port ? Number(url.port) : 443;
  if (!host || !Number.isInteger(port) || port < 1 || port > 65535) return undefined;
  return { host, port };
}

function parseHttpTarget(req, parentHelpers) {
  const target = req.url ?? '';
  let url;
  try {
    url = /^[A-Za-z][A-Za-z0-9+.-]*:\/\//.test(target)
      ? new URL(target)
      : new URL(target, `http://${req.headers.host ?? ''}`);
  } catch {
    return undefined;
  }
  if (url.protocol !== 'http:' || url.username || url.password || !url.hostname) return undefined;
  const host = parentHelpers.canonicalizeHost(parentHelpers.stripBrackets(url.hostname));
  const port = url.port ? Number(url.port) : 80;
  if (!host || !Number.isInteger(port) || port < 1 || port > 65535) return undefined;
  return {
    host,
    port,
    path: `${url.pathname || '/'}${url.search}`,
    originalAuthority: parentHelpers.formatAuthority(host, port, 80),
  };
}

/**
 * Make an authenticated loopback parent-proxy endpoint that pins each
 * destination to a freshly resolved, deny-checked IP and blocks protected
 * host/port endpoints before forwarding it.
 */
export async function createGuardedParentProxy({
  sdkRoot,
  deniedAddresses = [],
  protectedEndpoints: additionalProtectedEndpoints = [],
  onDeny = () => {},
  resolve = defaultResolve,
  parentConfig,
} = {}) {
  if (!sdkRoot) throw new TypeError('sdkRoot is required');
  const sdkProxyUrl = pathToFileURL(join(sdkRoot, 'dist', 'sandbox', 'parent-proxy.js')).href;
  const sdkAddressUrl = pathToFileURL(join(sdkRoot, 'dist', 'sandbox', 'address.js')).href;
  const parentHelpers = await import(sdkProxyUrl);
  const addressHelpers = await import(sdkAddressUrl);
  const required = ['resolveParentProxy', 'shouldBypassParentProxy', 'selectParentProxyUrl',
    'stripHopByHop', 'proxyAuthHeader',
    'canonicalizeHost', 'stripBrackets', 'formatAuthority', 'isValidHost', 'openConnectTunnel'];
  if (required.some(name => typeof parentHelpers[name] !== 'function')
      || typeof addressHelpers.isLoopbackAddress !== 'function') {
    throw new TypeError('SDK parent-proxy helpers are incomplete');
  }

  const denied = new Set();
  for (const value of deniedAddresses) {
    const normalized = parentHelpers.canonicalizeHost(String(value));
    if (!normalized || !isIP(normalized)) throw new TypeError('deniedAddresses must contain IP addresses');
    denied.add(normalized);
  }
  const parent = parentHelpers.resolveParentProxy(parentConfig);
  if (!parent) return { config: { ...EMPTY_PROXY }, close: async () => {}, protectedEndpoints: [] };

  if (!Array.isArray(additionalProtectedEndpoints)) {
    throw new TypeError('protectedEndpoints must be an array of {host,port} endpoints');
  }
  const protectedTargets = [];
  const addProtectedTarget = (endpoint) => {
    const existing = protectedTargets.find(value => sameEndpoint(value, endpoint));
    if (existing) {
      for (const address of endpoint.addresses) {
        if (!existing.addresses.includes(address)) existing.addresses.push(address);
      }
      return existing;
    }
    protectedTargets.push(endpoint);
    return endpoint;
  };
  const resolveProtectedAddresses = async host => {
    if (isIP(host)) return [host];
    return recordsToAddresses(await resolve(host), parentHelpers.canonicalizeHost);
  };
  const normalizeProtectedEndpoint = async endpoint => {
    if (!endpoint || typeof endpoint !== 'object' || typeof endpoint.host !== 'string') {
      throw new TypeError('protectedEndpoints entries require a host and port');
    }
    const host = parentHelpers.canonicalizeHost(parentHelpers.stripBrackets(endpoint.host));
    const port = Number(endpoint.port);
    if (!host || !Number.isInteger(port) || port < 1 || port > 65535) {
      throw new TypeError('protectedEndpoints entries require a valid host and port');
    }
    const addresses = endpoint.addresses === undefined
      ? await resolveProtectedAddresses(host)
      : recordsToAddresses(endpoint.addresses, parentHelpers.canonicalizeHost);
    return { host, port, addresses };
  };
  for (const endpoint of additionalProtectedEndpoints) {
    addProtectedTarget(await normalizeProtectedEndpoint(endpoint));
  }
  for (const parentUrl of [parent.httpUrl, parent.httpsUrl]) {
    if (!parentUrl) continue;
    const endpoint = protectedEndpoint(parentUrl, parentHelpers);
    addProtectedTarget({ ...endpoint, addresses: await resolveProtectedAddresses(endpoint.host) });
  }

  const clients = new Set();
  const upstreamSockets = new Set();
  const upstreamRequests = new Set();
  let closed = false;
  const proxyUser = 'lpac';
  const proxyPassword = randomBytes(32).toString('hex');
  const expectedProxyAuthorization = Buffer.from(`${proxyUser}:${proxyPassword}`, 'utf8');
  const proxyAuthenticate = 'Basic realm="SRT parent proxy shim"';

  const isAuthorized = value => {
    if (typeof value !== 'string') return false;
    const match = /^Basic\s+([A-Za-z0-9+/]+={0,2})$/i.exec(value.trim());
    if (!match) return false;
    let supplied;
    try {
      supplied = Buffer.from(match[1], 'base64');
    } catch {
      return false;
    }
    return supplied.length === expectedProxyAuthorization.length
      && timingSafeEqual(supplied, expectedProxyAuthorization);
  };

  const trackSocket = (socket, collection = upstreamSockets) => {
    collection.add(socket);
    socket.once('close', () => collection.delete(socket));
    socket.once('error', () => {});
    return socket;
  };
  const connectToParent = (proxyUrl, destHost, destPort) => parentHelpers.openConnectTunnel({
    destHost,
    destPort,
    authHeader: parentHelpers.proxyAuthHeader(proxyUrl),
    readyEvent: proxyUrl.protocol === 'https:' ? 'secureConnect' : 'connect',
    dial: () => {
      if (closed) throw new Error('Proxy is closed');
      const host = parentHelpers.stripBrackets(proxyUrl.hostname);
      const port = Number(proxyUrl.port) || (proxyUrl.protocol === 'https:' ? 443 : 80);
      const socket = proxyUrl.protocol === 'https:'
        ? tlsConnect({ host, port, ...(isIP(host) ? {} : { servername: host }) })
        : netConnect({ host, port });
      return trackSocket(socket);
    },
  });
  const connectDirect = (host, port) => new Promise((resolveConnect, rejectConnect) => {
    if (closed) return rejectConnect(new Error('Proxy is closed'));
    const socket = trackSocket(netConnect({ host, port }));
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      socket.setTimeout(0);
      socket.removeListener('connect', onConnect);
      socket.removeListener('error', onError);
      socket.removeListener('close', onClose);
      if (error) {
        socket.destroy();
        rejectConnect(error);
      } else {
        resolveConnect(socket);
      }
    };
    const onConnect = () => finish();
    const onError = error => finish(error);
    const onClose = () => finish(new Error('Connection closed during direct dial'));
    socket.once('connect', onConnect);
    socket.once('error', onError);
    socket.once('close', onClose);
    socket.setTimeout(30000, () => finish(new Error('Direct connection timed out')));
    if (closed) finish(new Error('Proxy is closed'));
  });
  const resolveTarget = async (host, port) => {
    const addresses = recordsToAddresses(await resolve(host), parentHelpers.canonicalizeHost);
    const canonicalHost = parentHelpers.canonicalizeHost(host);
    const protectedEndpoint = protectedTargets.find(endpoint => endpoint.port === port
      && (endpoint.host === canonicalHost || addresses.some(address => endpoint.addresses.includes(address)
        || (addressHelpers.isLoopbackAddress(address)
          && endpoint.addresses.some(addressHelpers.isLoopbackAddress)))));
    if (protectedEndpoint) {
      try {
        await onDeny({ host, port, addresses: [...addresses], reason: 'protected_endpoint' });
      } catch {
        // Infrastructure denial remains effective even if diagnostics fail.
      }
      return { denied: true, addresses, reason: 'protected_endpoint' };
    }
    if (addresses.some(address => denied.has(address))) {
      try {
        await onDeny({ host, addresses: [...addresses], reason: 'denied_address' });
      } catch {
        // Denial must remain effective even if a diagnostic callback fails.
      }
      return { denied: true, addresses };
    }
    return { denied: false, address: addresses[0], addresses };
  };
  const routeParent = (host, isHttps) => {
    if (parentHelpers.shouldBypassParentProxy(parent, host)) return undefined;
    return parentHelpers.selectParentProxyUrl(parent, { isHttps });
  };
  const selectedAddressAuthority = (address, port, protocol) =>
    parentHelpers.formatAuthority(address, port, defaultPort(protocol));

  const server = http.createServer();
  server.on('connection', socket => trackSocket(socket, clients));

  server.on('request', async (req, res) => {
    req.on('error', () => {});
    res.on('error', () => res.socket?.destroy());
    if (closed) return response(res, 503, 'Proxy is closed');
    if (!isAuthorized(req.headers['proxy-authorization'])) {
      return response(res, 407, 'Proxy authentication required', { 'proxy-authenticate': proxyAuthenticate });
    }
    const target = parseHttpTarget(req, parentHelpers);
    if (!target) return response(res, 400, 'Invalid HTTP proxy target');
    try {
      const checked = await resolveTarget(target.host, target.port);
      if (closed || req.socket.destroyed || res.destroyed) return;
      if (checked.denied) return response(res, 403, 'Destination denied');

      const proxyUrl = routeParent(target.host, false);
      const fixedAuthority = selectedAddressAuthority(checked.address, target.port, 'http:');
      const absolutePath = `http://${fixedAuthority}${target.path}`;
      const headers = {
        ...parentHelpers.stripHopByHop(req.headers),
        host: target.originalAuthority,
      };
      const declaredBody = Boolean(req.headers['content-length'] || req.headers['transfer-encoding']);
      if (declaredBody && headers['content-length'] === undefined
          && headers['transfer-encoding'] === undefined) {
        headers['transfer-encoding'] = 'chunked';
      }
      let requestOptions;
      let requestPath;
      let requestHeaders = headers;
      let requestFn;
      if (proxyUrl) {
        requestFn = proxyUrl.protocol === 'https:' ? https.request : http.request;
        requestOptions = {
          hostname: parentHelpers.stripBrackets(proxyUrl.hostname),
          port: Number(proxyUrl.port) || (proxyUrl.protocol === 'https:' ? 443 : 80),
        };
        requestPath = absolutePath;
        const authorization = parentHelpers.proxyAuthHeader(proxyUrl);
        if (authorization) requestHeaders = { ...headers, 'proxy-authorization': authorization };
      } else {
        requestFn = http.request;
        requestOptions = { hostname: checked.address, port: target.port };
        requestPath = target.path;
      }
      const upstream = requestFn({ ...requestOptions, method: req.method, path: requestPath,
        headers: requestHeaders, agent: false }, upstreamResponse => {
        upstreamResponse.on('error', () => res.destroy());
        if (!res.destroyed) res.writeHead(upstreamResponse.statusCode ?? 502,
          parentHelpers.stripHopByHop(upstreamResponse.headers));
        upstreamResponse.pipe(res);
      });
      upstreamRequests.add(upstream);
      upstream.once('close', () => upstreamRequests.delete(upstream));
      upstream.once('socket', socket => {
        trackSocket(socket);
        if (closed) socket.destroy();
      });
      const abort = () => upstream.destroy();
      res.once('close', abort);
      req.once('aborted', abort);
      upstream.on('error', () => {
        if (!res.headersSent && !res.destroyed) response(res, 502, 'Upstream request failed');
        else res.destroy();
      });
      req.pipe(upstream);
    } catch {
      if (!res.destroyed && !res.headersSent) response(res, 502, 'Destination resolution failed');
      else res.destroy();
    }
  });

  server.on('connect', (req, clientSocket, head) => {
    if (!closed && !isAuthorized(req.headers['proxy-authorization'])) {
      return connectResponse(clientSocket, 407, 'Proxy authentication required');
    }
    (async () => {
      if (closed || clientSocket.destroyed) return;
      const target = parseConnectTarget(req.url, parentHelpers);
      if (!target || !parentHelpers.isValidHost(target.host)) {
        return connectResponse(clientSocket, 400, 'Invalid target');
      }
      const checked = await resolveTarget(target.host, target.port);
      if (closed || clientSocket.destroyed) return;
      if (checked.denied) return connectResponse(clientSocket, 403, 'Destination denied');
      let upstream;
      const proxyUrl = routeParent(target.host, true);
      if (proxyUrl) {
        upstream = await connectToParent(proxyUrl, checked.address, target.port);
      } else {
        upstream = await connectDirect(checked.address, target.port);
      }
      if (closed || clientSocket.destroyed || upstream.destroyed) {
        upstream.destroy();
        return;
      }
      clientSocket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      clientSocket.allowHalfOpen = true;
      upstream.allowHalfOpen = true;
      if (head.length) upstream.write(head);
      clientSocket.pipe(upstream);
      upstream.pipe(clientSocket);
      const closeUpstream = () => upstream.destroy();
      const closeClient = () => clientSocket.destroy();
      clientSocket.once('close', closeUpstream);
      upstream.once('close', closeClient);
      clientSocket.once('error', closeUpstream);
      upstream.once('error', closeClient);
    })().catch(() => {
      connectResponse(clientSocket, 502, 'Upstream connection failed');
    });
  });

  let address;
  for (let attempt = 0; attempt < 32; attempt++) {
    await new Promise((resolveListen, rejectListen) => {
      server.once('error', rejectListen);
      server.listen(0, '127.0.0.1', () => {
        server.removeListener('error', rejectListen);
        resolveListen();
      });
    });
    address = server.address();
    if (address && typeof address !== 'string' && !LPAC_PROXY_PORTS.has(address.port)) break;
    await new Promise(resolveClose => server.close(resolveClose));
    address = undefined;
  }
  if (!address || typeof address === 'string') {
    await new Promise(resolveClose => server.close(resolveClose));
    throw new Error('Could not determine parent-proxy shim address');
  }
  const authenticatedShimUrl = `http://${encodeURIComponent(proxyUser)}:${proxyPassword}@127.0.0.1:${address.port}`;
  const config = { http: authenticatedShimUrl, https: authenticatedShimUrl, noProxy: '' };
  const protectedEndpoints = [{ host: '127.0.0.1', port: address.port }];
  addProtectedTarget({ host: '127.0.0.1', port: address.port, addresses: ['127.0.0.1'] });
  for (const parentUrl of [parent.httpUrl, parent.httpsUrl]) {
    if (!parentUrl) continue;
    const endpoint = protectedEndpoint(parentUrl, parentHelpers);
    if (!protectedEndpoints.some(existing => sameEndpoint(existing, endpoint))) protectedEndpoints.push(endpoint);
  }

  return {
    config,
    protectedEndpoints,
    async close() {
      if (closed) return;
      closed = true;
      for (const request of upstreamRequests) request.destroy();
      for (const socket of clients) socket.destroy();
      for (const socket of upstreamSockets) socket.destroy();
      await new Promise(resolveClose => {
        try {
          server.close(() => resolveClose());
        } catch {
          resolveClose();
        }
      });
      for (const socket of clients) socket.destroy();
      for (const socket of upstreamSockets) socket.destroy();
    },
  };
}
