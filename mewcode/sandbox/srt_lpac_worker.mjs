// Host broker: SRT proxies only. Every task process keeps its LPAC token.
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import readline from 'node:readline';
import { isIP } from 'node:net';
import { lookup } from 'node:dns/promises';
import { createGuardedParentProxy } from './srt_parent_proxy.mjs';
const lines = readline.createInterface({ input: process.stdin });
const input = JSON.parse((await lines[Symbol.asyncIterator]().next()).value || '{}');
const root = process.argv[2];
if (JSON.parse(readFileSync(root + '/package.json')).version !== '0.0.77') throw new Error('SRT version mismatch');
const lib = await import(pathToFileURL(root + '/dist/index.js').href);
const utils = await import(pathToFileURL(root + '/dist/sandbox/sandbox-utils.js').href);
const { canonicalizeHost } = await import(pathToFileURL(root + '/dist/sandbox/parent-proxy.js').href);
const { isLoopbackAddress } = await import(pathToFileURL(root + '/dist/sandbox/address.js').href);
const srtWin = { exe: root + '/vendor/srt-win/' + process.arch + '/srt-win.exe', prependArgs: [] };
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const result = { schema: 'mewcode-srt-lpac-receipt-v1', request_id: input.request_id,
  cleanup_confirmed: true, native_receipt: null, native_exit_code: null,
  command_started: false, error_code: '', detail: '' };
let initialized = false;
let guardedParent;
const verify = item => {
  if (createHash('sha256').update(readFileSync(item.path)).digest('hex') !== item.sha256)
    throw new Error('native_hash_mismatch');
};
const launch = async (base64, env, probe) => {
  verify(input.native_helper);
  result.cleanup_confirmed = false;
  const child = spawn(input.native_helper.path, ['--host-network-stdin-v1'],
    { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  let chunks = [], bytes = 0, pending = '';
  child.stdout.on('data', data => { bytes += data.length; if (bytes <= 8 * 1024 * 1024) chunks.push(data); });
  child.stderr.on('data', data => {
    pending += data.toString('utf8'); let at;
    while ((at = pending.indexOf('\n')) >= 0) {
      const row = pending.slice(0, at + 1); pending = pending.slice(at + 1);
      if (/^MEWCODE_CHILD_PID=[1-9][0-9]*\r?\n$/.test(row)) {
        if (probe) process.stderr.write(row.replace('MEWCODE_CHILD_PID', 'MEWCODE_NETWORK_PROBE_PID'));
        else { result.command_started = true; process.stderr.write(row); }
      }
    }
    if (pending.length > 256) pending = '';
  });
  await once(child, 'spawn');
  child.stdin.end(JSON.stringify({ schema: 'mewcode-lpac-network-transport-v1',
    request_base64: base64, proxy_environment: env }));
  const [code] = await once(child, 'close');
  if (bytes > 8 * 1024 * 1024) throw new Error('native_receipt_oversized');
  const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (value.schema !== 'mewcode-lpac-receipt-v1') throw new Error('native_receipt_invalid');
  return { value, code };
};
const clean = receipt => ['job_cleanup_ok', 'acl_cleanup_ok', 'profile_cleanup_ok'].every(key => receipt[key] === true);
try {
  if (input.operation === 'status') {
    const user = await lib.getWindowsSandboxUserStatusAsync({ srtWin });
    const wfp = await lib.getWindowsWfpStatusAsync({ srtWin });
    emit({ provisioned: user.provisioned && user.credPresent, sandbox_sid: user.sid,
      sandbox_group_sid: user.groupSid, wfp: wfp.state, version: '0.0.77' });
    lines.close(); process.exit(0);
  }
  if (input.operation === 'install') {
    const value = await lib.installWindowsSandboxAsync({ srtWin });
    emit({ installed: !value.cancelled && value.user?.provisioned === true, cancelled: value.cancelled === true });
    lines.close(); process.exit(0);
  }
  if (input.operation !== 'execute' || !input.native_helper || !input.probe_helper) throw new Error('unsupported_host_request');
  verify(input.probe_helper);
  const policy = input.network;
  if (typeof policy?.enabled !== 'boolean') throw new Error('policy_missing');
  const denied = policy.denied_domains.flatMap(host => isIP(host) ? [isIP(canonicalizeHost(host)) === 6 ? `[${canonicalizeHost(host)}]` : canonicalizeHost(host)]
    : host === 'localhost' ? [host] : host.startsWith('*.') ? [host.slice(2), host] : [host, '*.' + host]);
  const addresses = async host => { host = canonicalizeHost(host); if (!host) return [];
    if (isIP(host)) return [host];
    try { return (await lookup(host, { all: true })).map(v => canonicalizeHost(v.address)); } catch { return []; } };
  const ips = new Set();
  for (const host of policy.denied_domains)
    for (const ip of await addresses(host.replace(/^\*\./, ''))) ips.add(ip);
  denied.push(...[...ips].map(ip => isIP(ip) === 6 ? `[${ip}]` : ip));
  const denials = [];
  const protectedEndpoints = [{ host: '127.0.0.1', port: 60080, addresses: ['127.0.0.1'] },
                              { host: '127.0.0.1', port: 60081, addresses: ['127.0.0.1'] }];
  const allow = async ({ host, port }) => {
    const target = await addresses(host);
    const infrastructure = protectedEndpoints.some(endpoint => endpoint.port === port &&
      (endpoint.host === canonicalizeHost(host) || target.some(ip => endpoint.addresses.includes(ip)
        || (isLoopbackAddress(ip) && endpoint.addresses.some(isLoopbackAddress)))));
    if (infrastructure) {
      denials.push(`deny network-outbound ${host}:${port} (NETWORK_INFRASTRUCTURE_TARGET: 不能通过任务代理访问代理维护入口)`);
      return false;
    }
    const hit = target.some(ip => ips.has(ip));
    if (hit) denials.push(`deny network-outbound ${host}:${port} (NETWORK_BLACKLISTED: 目标地址命中黑名单)`);
    return policy.enabled && !hit;
  };
  guardedParent = await createGuardedParentProxy({ sdkRoot: root, deniedAddresses: [...ips], protectedEndpoints,
    onDeny: ({ host }) => denials.push(`deny network-outbound ${host} (NETWORK_BLACKLISTED: 目标地址命中黑名单)`) });
  for (const endpoint of guardedParent.protectedEndpoints)
    protectedEndpoints.push({ ...endpoint, host: canonicalizeHost(endpoint.host), addresses: await addresses(endpoint.host) });
  initialized = true;
  await lib.SandboxManager.initialize({ network: {
    allowedDomains: input.test_hosts || [], deniedDomains: policy.enabled ? denied : ['*'],
    deniedDomainReasons: Object.fromEntries((policy.enabled ? denied : ['*']).map(host =>
      [host, policy.enabled ? `NETWORK_BLACKLISTED: ${host}` : 'NETWORK_DISABLED: 任务联网已关闭'])),
    deniedResolvedAddresses: [...ips], parentProxy: guardedParent.config,
    strictAllowlist: false, allowLocalBinding: false },
    filesystem: { disabled: true, allowWrite: [], denyWrite: [], denyRead: [] },
    windows: { srtWin: { path: srtWin.exe }, proxyPortRange: [60080, 60081] },
    enableWeakerNetworkIsolation: false, enableWeakerNestedSandbox: false }, allow);
  if (lib.SandboxManager.getProxyPort() !== 60081 || lib.SandboxManager.getSocksProxyPort() !== 60081)
    throw new Error('proxy_ports_mismatch');
  const accepted = new Set(['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'GRPC_PROXY', 'GIT_CONFIG_PARAMETERS']);
  const env = { NO_PROXY: '' };
  for (const row of utils.generateProxyEnvVars(60081, 60081, undefined,
      lib.SandboxManager.getProxyAuthToken(), undefined, undefined, true)) {
    const at = row.indexOf('='), key = row.slice(0, at).toUpperCase();
    if (accepted.has(key)) env[key] = row.slice(at + 1).replaceAll('localhost:60081', '127.0.0.1:60081');
  }
  console.error('MEW_SRT_STAGE: network preflight');
  const probe = await launch(input.probe_envelope_base64, env, true);
  result.cleanup_confirmed = clean(probe.value.receipt);
  let observation;
  try { observation = JSON.parse(probe.value.receipt.stdout.trim()); }
  catch { observation = { ready: false, native_probe_error: probe.value.receipt.error,
    probe_stdout: probe.value.receipt.stdout.slice(0, 4000), probe_stderr: probe.value.receipt.stderr.slice(0, 4000),
    probe_exit_code: probe.value.receipt.exit_code }; }
  result.preflight = observation;
  if (probe.code !== 0 || !result.cleanup_confirmed || observation.ready !== true
      || observation.package_sid !== input.native_helper.profile_sid) {
    result.error_code = 'network_fence_unavailable';
    result.detail = 'NETWORK_RUNTIME_UNAVAILABLE: AppContainer 出网隔离探针未通过，原任务命令未启动。';
  } else {
    console.error('MEW_SRT_STAGE: task command');
    result.cleanup_confirmed = false;
    const task = await launch(input.envelope_base64, env, false);
    result.native_receipt = task.value; result.native_exit_code = task.code;
    result.cleanup_confirmed = clean(task.value.receipt);
    result.network_denials = [...lib.SandboxManager.getSandboxViolationStore().getViolations()
      .filter(v => v.line.startsWith('deny network-outbound ')).map(v => v.line), ...denials];
  }
} catch (error) {
  result.error_code = result.error_code || 'network_runtime_unavailable';
  const detail = String(error?.message || '未知运行器错误').replace(/:\/\/[^/\s]+@/g, '://[redacted]@').slice(0, 1200);
  result.detail = result.detail || `NETWORK_RUNTIME_UNAVAILABLE: 联网执行器未完成调用：${detail}`;
} finally {
  if (initialized) { try { await lib.SandboxManager.reset(); }
    catch { result.cleanup_confirmed = false; result.error_code = 'srt_cleanup_unconfirmed'; } }
  if (guardedParent) { try { await guardedParent.close(); }
    catch { result.cleanup_confirmed = false; result.error_code = 'srt_cleanup_unconfirmed'; } }
  lines.close();
}
emit(result);
