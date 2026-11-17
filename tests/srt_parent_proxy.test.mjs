import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import test from 'node:test';
import { createGuardedParentProxy } from '../mewcode/sandbox/srt_parent_proxy.mjs';

const sdkRoot = process.env.MEWCODE_SRT_SDK_ROOT
  ?? path.join(process.env.APPDATA ?? '', 'npm', 'node_modules', '@anthropic-ai', 'sandbox-runtime');

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return server.address().port;
}

function trackedServer(server, sockets) {
  server.on('connection', socket => {
    sockets.add(socket);
    socket.once('close', () => sockets.delete(socket));
  });
}

async function closeServer(server, sockets) {
  for (const socket of sockets) socket.destroy();
  await new Promise(resolve => {
    try { server.close(resolve); } catch { resolve(); }
  });
}

function makeParent() {
  const requests = [];
  const tunnels = [];
  const sockets = new Set();
  const server = http.createServer((req, res) => {
    requests.push({ method: req.method, url: req.url, headers: req.headers });
    req.resume();
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'text/plain' });
      res.end('parent-ok');
    });
  });
  trackedServer(server, sockets);
  server.on('connect', (req, socket, head) => {
    const tunnel = { authority: req.url, headers: req.headers, bytes: Buffer.alloc(0), ended: false, socket };
    tunnels.push(tunnel);
    socket.on('data', chunk => {
      tunnel.bytes = Buffer.concat([tunnel.bytes, chunk]);
      socket.write(chunk);
    });
    socket.once('end', () => { tunnel.ended = true; });
    socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
    if (head.length) socket.write(head);
  });
  return { server, sockets, requests, tunnels };
}

function authorizationFor(proxyUrl) {
  const { username, password } = new URL(proxyUrl);
  return `Basic ${Buffer.from(`${decodeURIComponent(username)}:${decodeURIComponent(password)}`).toString('base64')}`;
}

async function getThroughShim(proxyUrl, target, headers = {}, authorization = authorizationFor(proxyUrl)) {
  const proxy = new URL(proxyUrl);
  return await new Promise((resolve, reject) => {
    const requestHeaders = { ...headers };
    if (authorization !== null) requestHeaders['proxy-authorization'] = authorization;
    const request = http.request({ hostname: '127.0.0.1', port: Number(proxy.port), method: 'GET',
      path: target, headers: requestHeaders }, response => {
      const chunks = [];
      response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => resolve({ status: response.statusCode,
        headers: response.headers, body: Buffer.concat(chunks).toString('utf8') }));
    });
    request.once('error', reject);
    request.end();
  });
}

async function connectThroughShim(proxyUrl, authority, authorization = authorizationFor(proxyUrl)) {
  const proxy = new URL(proxyUrl);
  const socket = net.connect(Number(proxy.port), '127.0.0.1');
  const response = await new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0);
    socket.once('error', reject);
    socket.once('connect', () => socket.write(`CONNECT ${authority} HTTP/1.1\r\nHost: ${authority}\r\n${authorization === null ? '' : `Proxy-Authorization: ${authorization}\r\n`}\r\n`));
    const onData = chunk => {
      buffer = Buffer.concat([buffer, chunk]);
      const end = buffer.indexOf('\r\n\r\n');
      if (end < 0) return;
      socket.removeListener('data', onData);
      const header = buffer.subarray(0, end).toString('latin1');
      const status = Number(header.split(' ', 2)[1]);
      const rest = buffer.subarray(end + 4);
      resolve({ status, header, socket, rest });
    };
    socket.on('data', onData);
  });
  if (response.rest.length) response.socket.unshift(response.rest);
  return response;
}

async function waitFor(check, timeoutMs = 1500) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    if (check()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.ok(check(), 'timed out waiting for local proxy activity');
}

test('plain HTTP parent route pins the absolute URI to an IP and preserves Host', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { http: `http://user:secret@127.0.0.1:${parentPort}`, https: `http://user:secret@127.0.0.1:${parentPort}` },
    deniedAddresses: ['203.0.113.99'],
    resolve: async host => {
      assert.equal(host, 'plain.example.test');
      return ['203.0.113.12'];
    },
  });
  t.after(() => guarded.close());

  assert.equal(new URL(guarded.config.http).username, 'lpac');
  assert.equal(new URL(guarded.config.http).password.length, 64);
  assert.equal(guarded.config.noProxy, '');
  assert.ok(![60080, 60081].includes(Number(new URL(guarded.config.http).port)));
  const result = await getThroughShim(guarded.config.http,
    'http://plain.example.test:8080/a/b?x=1', { host: 'plain.example.test:8080', connection: 'keep-alive' });
  assert.equal(result.status, 200);
  assert.equal(parent.requests.length, 1);
  assert.equal(parent.requests[0].url, 'http://203.0.113.12:8080/a/b?x=1');
  assert.equal(parent.requests[0].headers.host, 'plain.example.test:8080');
  assert.equal(parent.requests[0].headers['proxy-authorization'], `Basic ${Buffer.from('user:secret').toString('base64')}`);
  assert.equal(parent.requests[0].headers.connection, 'close');
  assert.ok(guarded.protectedEndpoints.some(({ host, port }) => host === '127.0.0.1' && port === Number(new URL(guarded.config.http).port)));
  assert.ok(guarded.protectedEndpoints.some(({ host, port }) => host === '127.0.0.1' && port === parentPort));
});

test('missing and incorrect Basic credentials return 407 before DNS or parent I/O', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  let lookups = 0;
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { http: `http://127.0.0.1:${parentPort}` },
    resolve: async () => { lookups++; return ['203.0.113.56']; },
  });
  t.after(() => guarded.close());

  for (const authorization of [null, 'Basic YWRtaW46cGFzcw==']) {
    const httpResult = await getThroughShim(guarded.config.http,
      'http://auth-check.example.test/probe', { host: 'auth-check.example.test' }, authorization);
    assert.equal(httpResult.status, 407);
    assert.match(httpResult.headers['proxy-authenticate'], /^Basic /);
    const connectResult = await connectThroughShim(guarded.config.https,
      'auth-check.example.test:443', authorization);
    assert.equal(connectResult.status, 407);
    assert.match(connectResult.header, /Proxy-Authenticate: Basic /i);
    connectResult.socket.destroy();
  }
  assert.equal(lookups, 0);
  assert.equal(parent.requests.length, 0);
  assert.equal(parent.tunnels.length, 0);
});

test('the shim rejects a rebinding target that resolves to the frozen parent proxy address', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  let targetLookups = 0;
  let parentLookups = 0;
  const resolve = async host => {
    if (host === 'parent-proxy.example.test') {
      parentLookups++;
      return ['127.0.0.1'];
    }
    assert.equal(host, 'rebind.example.test');
    targetLookups++;
    return targetLookups === 1 ? ['203.0.113.77'] : ['127.0.0.1'];
  };
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { https: `http://parent-proxy.example.test:${parentPort}` },
    resolve,
  });
  t.after(() => guarded.close());

  // Simulate the worker's earlier preflight lookup followed by the shim's
  // independent lookup after a DNS rebinding change.
  assert.deepEqual(await resolve('rebind.example.test'), ['203.0.113.77']);
  const result = await connectThroughShim(guarded.config.https,
    `rebind.example.test:${parentPort}`);
  assert.equal(result.status, 403);
  assert.equal(targetLookups, 2);
  assert.equal(parentLookups, 1, 'the parent proxy hostname should be resolved and frozen once');
  assert.equal(parent.tunnels.length, 0);
  assert.equal(parent.requests.length, 0);
  result.socket.destroy();
});

test('each request is resolved again and uses only that request’s pinned address', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  const answers = ['203.0.113.31', '203.0.113.32'];
  let lookups = 0;
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { http: `http://127.0.0.1:${parentPort}` },
    resolve: async () => [answers[lookups++]],
  });
  t.after(() => guarded.close());

  for (const pathPart of ['/first', '/second']) {
    const result = await getThroughShim(guarded.config.http,
      `http://fresh.example.test${pathPart}`, { host: 'fresh.example.test' });
    assert.equal(result.status, 200);
  }
  assert.equal(lookups, 2);
  assert.deepEqual(parent.requests.map(row => row.url), [
    'http://203.0.113.31/first', 'http://203.0.113.32/second',
  ]);
});

test('CONNECT uses the fixed resolved IP while the tunnel payload remains opaque', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { http: `http://u:p@127.0.0.1:${parentPort}`, https: `http://u:p@127.0.0.1:${parentPort}` },
    resolve: async host => {
      assert.equal(host, 'secure.example.test');
      return ['2001:0db8:0:0:0:0:0:7'];
    },
  });
  t.after(() => guarded.close());

  const tunnel = await connectThroughShim(guarded.config.https, 'secure.example.test:443');
  assert.equal(tunnel.status, 200);
  assert.equal(parent.tunnels.length, 1);
  assert.equal(parent.tunnels[0].authority, '[2001:db8::7]:443');
  assert.equal(parent.tunnels[0].headers['proxy-authorization'], `Basic ${Buffer.from('u:p').toString('base64')}`);
  const opaqueClientHello = Buffer.from([0x16, 0x03, 0x01, 0x00, 0x03, 0x53, 0x4e, 0x49]);
  tunnel.socket.write(opaqueClientHello);
  await waitFor(() => parent.tunnels[0].bytes.length >= opaqueClientHello.length);
  assert.deepEqual(parent.tunnels[0].bytes, opaqueClientHello);
  tunnel.socket.destroy();
});

test('a DNS set with any denied IP returns 403 without contacting the parent', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  const denials = [];
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { http: `http://127.0.0.1:${parentPort}` },
    deniedAddresses: ['198.51.100.22'],
    resolve: async () => ['203.0.113.22', '198.51.100.22'],
    onDeny: event => denials.push(event),
  });
  t.after(() => guarded.close());

  const result = await getThroughShim(guarded.config.http,
    'http://blocked.example.test/private', { host: 'blocked.example.test' });
  assert.equal(result.status, 403);
  assert.equal(parent.requests.length, 0);
  assert.deepEqual(denials, [{ host: 'blocked.example.test',
    addresses: ['203.0.113.22', '198.51.100.22'], reason: 'denied_address' }]);
});

test('expanded IPv6 and IPv4-mapped IPv6 addresses match the denied set', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { https: `http://127.0.0.1:${parentPort}` },
    deniedAddresses: ['2001:db8::8', '192.0.2.45'],
    resolve: async host => host === 'mapped.test'
      ? ['::ffff:192.0.2.45'] : ['2001:0db8:0:0:0:0:0:8'],
  });
  t.after(() => guarded.close());

  const ipv6 = await connectThroughShim(guarded.config.https, 'expanded.test:443');
  const mapped = await connectThroughShim(guarded.config.https, 'mapped.test:443');
  assert.equal(ipv6.status, 403);
  assert.equal(mapped.status, 403);
  assert.equal(parent.tunnels.length, 0);
});

test('caller-protected SRT ports and shim port reject loopback aliases', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { https: `http://127.0.0.1:${parentPort}` },
    protectedEndpoints: [
      { host: '127.0.0.1', port: 60080, addresses: ['127.0.0.1'] },
      { host: '127.0.0.1', port: 60081, addresses: ['127.0.0.1'] },
    ],
    resolve: async host => {
      if (host === 'ipv6-loopback-alias.test') return ['::1'];
      if (host === 'ipv4-loopback-alias.test' || host === 'shim-loopback-alias.test') return ['127.0.0.2'];
      throw new Error(`unexpected host: ${host}`);
    },
  });
  t.after(() => guarded.close());

  const shimPort = Number(new URL(guarded.config.https).port);
  for (const authority of [
    'ipv6-loopback-alias.test:60080',
    'ipv4-loopback-alias.test:60081',
    `shim-loopback-alias.test:${shimPort}`,
  ]) {
    const result = await connectThroughShim(guarded.config.https, authority);
    assert.equal(result.status, 403, authority);
    result.socket.destroy();
  }
  assert.equal(parent.tunnels.length, 0);
});

test('an external protected endpoint does not blacklist a different loopback destination at the same port', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  const targetRequests = [];
  const targetSockets = new Set();
  const target = http.createServer((req, res) => {
    targetRequests.push(req.url);
    res.end('allowed-loopback');
  });
  trackedServer(target, targetSockets);
  const targetPort = await listen(target);
  t.after(() => closeServer(target, targetSockets));
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { http: `http://127.0.0.1:${parentPort}` },
    protectedEndpoints: [{ host: 'protected.example.test', port: targetPort,
      addresses: ['198.51.100.90'] }],
    resolve: async host => {
      assert.equal(host, '127.0.0.1');
      return ['127.0.0.1'];
    },
  });
  t.after(() => guarded.close());

  const result = await getThroughShim(guarded.config.http,
    `http://127.0.0.1:${targetPort}/allowed`,
    { host: `127.0.0.1:${targetPort}` });
  assert.equal(result.status, 200);
  assert.equal(result.body, 'allowed-loopback');
  assert.deepEqual(targetRequests, ['/allowed']);
  assert.equal(parent.requests.length, 0);
});

test('NO_PROXY routes through the resolved fixed IP directly', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  const targetRequests = [];
  const targetSockets = new Set();
  const target = http.createServer((req, res) => {
    targetRequests.push({ url: req.url, host: req.headers.host });
    res.end('direct-ok');
  });
  trackedServer(target, targetSockets);
  const targetPort = await listen(target);
  t.after(() => closeServer(target, targetSockets));
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { http: `http://127.0.0.1:${parentPort}`, noProxy: 'direct.example.test' },
    resolve: async host => {
      assert.equal(host, 'direct.example.test');
      return ['127.0.0.1'];
    },
  });
  t.after(() => guarded.close());

  const result = await getThroughShim(guarded.config.http,
    `http://direct.example.test:${targetPort}/direct`, { host: `direct.example.test:${targetPort}` });
  assert.equal(result.status, 200);
  assert.equal(result.body, 'direct-ok');
  assert.deepEqual(targetRequests, [{ url: '/direct', host: `direct.example.test:${targetPort}` }]);
  assert.equal(parent.requests.length, 0);
});

test('a missing scheme-specific parent route connects directly to the pinned IP', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  const targetRequests = [];
  const targetSockets = new Set();
  const target = http.createServer((req, res) => {
    targetRequests.push(req.headers.host);
    res.end('direct-only');
  });
  trackedServer(target, targetSockets);
  const targetPort = await listen(target);
  t.after(() => closeServer(target, targetSockets));
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { http: '', https: `http://127.0.0.1:${parentPort}` },
    resolve: async () => ['127.0.0.1'],
  });
  t.after(() => guarded.close());

  const result = await getThroughShim(guarded.config.http,
    `http://http-only.example.test:${targetPort}/direct`,
    { host: `http-only.example.test:${targetPort}` });
  assert.equal(result.status, 200);
  assert.equal(result.body, 'direct-only');
  assert.deepEqual(targetRequests, [`http-only.example.test:${targetPort}`]);
  assert.equal(parent.requests.length, 0);
});

test('close destroys established CONNECT client and parent sockets', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { https: `http://127.0.0.1:${parentPort}` },
    resolve: async () => ['203.0.113.73'],
  });
  const tunnel = await connectThroughShim(guarded.config.https, 'cancel.example.test:443');
  assert.equal(tunnel.status, 200);
  await guarded.close();
  await waitFor(() => tunnel.socket.destroyed);
  await waitFor(() => parent.tunnels[0].ended);
  t.after(() => guarded.close());
});

test('close cancels a request stalled in DNS before any upstream dial', async t => {
  const parent = makeParent();
  const parentPort = await listen(parent.server);
  t.after(() => closeServer(parent.server, parent.sockets));
  let beginResolve;
  let finishResolve;
  const resolverStarted = new Promise(resolve => { beginResolve = resolve; });
  const resolverGate = new Promise(resolve => { finishResolve = resolve; });
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { http: `http://127.0.0.1:${parentPort}` },
    resolve: async () => {
      beginResolve();
      await resolverGate;
      return ['127.0.0.1'];
    },
  });
  const request = http.request({ hostname: '127.0.0.1', port: new URL(guarded.config.http).port,
    method: 'GET', path: 'http://cancel-dns.example.test/wait',
    headers: { host: 'cancel-dns.example.test', 'proxy-authorization': authorizationFor(guarded.config.http) } }, response => response.resume());
  request.on('error', () => {});
  request.end();
  await resolverStarted;
  const startedAt = Date.now();
  await guarded.close();
  assert.ok(Date.now() - startedAt < 1000, 'close should not wait for the DNS resolver');
  finishResolve();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(parent.requests.length, 0);
  t.after(() => guarded.close());
});

test('close cancels a parent CONNECT handshake without waiting for the SDK timeout', async t => {
  const parentSockets = new Set();
  const parentAccepted = [];
  const parent = http.createServer();
  trackedServer(parent, parentSockets);
  parent.on('connect', (_req, socket) => {
    parentAccepted.push(socket);
    // Deliberately leave CONNECT unanswered so the shim is waiting in the
    // SDK's upstream handshake when close is called.
  });
  const parentPort = await listen(parent);
  t.after(() => closeServer(parent, parentSockets));
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { https: `http://127.0.0.1:${parentPort}` },
    resolve: async () => ['203.0.113.74'],
  });
  t.after(() => guarded.close());

  const proxy = new URL(guarded.config.https);
  const proxyPort = Number(proxy.port);
  const client = net.connect(proxyPort, '127.0.0.1');
  client.on('error', () => {});
  await new Promise((resolve, reject) => {
    client.once('connect', () => {
      client.write(`CONNECT stalled.example.test:443 HTTP/1.1\r\nHost: stalled.example.test:443\r\nProxy-Authorization: ${authorizationFor(guarded.config.https)}\r\n\r\n`);
      resolve();
    });
    client.once('error', reject);
  });
  await waitFor(() => parentAccepted.length === 1);

  const destroyedSockets = [];
  const originalDestroy = net.Socket.prototype.destroy;
  net.Socket.prototype.destroy = function (...args) {
    if (this.localPort === proxyPort || this.remotePort === parentPort) destroyedSockets.push(this);
    return originalDestroy.apply(this, args);
  };
  const startedAt = Date.now();
  try {
    await guarded.close();
  } finally {
    net.Socket.prototype.destroy = originalDestroy;
  }
  assert.ok(Date.now() - startedAt < 1000, 'close should cancel a stalled parent CONNECT promptly');
  assert.ok(destroyedSockets.some(socket => socket.remotePort === parentPort && socket.destroyed),
    'close should destroy the pending parent socket');
  assert.ok(destroyedSockets.some(socket => socket.remotePort !== parentPort && socket.destroyed),
    'close should destroy the shim-side client socket');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(parentAccepted.length, 1, 'close must not start another parent connection');
});

test('no host proxy config returns explicit empty values and opens no shim', async () => {
  const guarded = await createGuardedParentProxy({ sdkRoot,
    parentConfig: { http: '', https: '', noProxy: '' },
    deniedAddresses: ['192.0.2.1'],
  });
  assert.deepEqual(guarded.config, { http: '', https: '', noProxy: '' });
  assert.deepEqual(guarded.protectedEndpoints, []);
  await guarded.close();
});
