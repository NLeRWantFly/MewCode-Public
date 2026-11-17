// Host-owned task data, with display compatibility for older workflow hosts.
import {alignWorkflowView} from './workflow-summary.js?v=20261117-exploration';
export class ApiError extends Error {
  constructor(message, status, payload = {}) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

export const VIEW_ERROR = '无法读取最新进度，显示最后记录。';

export function hostPath(path) {
  const base=globalThis.window?.MewWorkbenchHost?.apiBase||'';
  return /^\/api\/workbench\/connections\/[a-f0-9]{32}$/.test(base)&&path.startsWith('/api/workbench/')?base+path:path;
}

export function createAPI(fetcher = globalThis.fetch.bind(globalThis)) {
  async function request(path, {body, signal, query, method, headers = {}} = {}) {
    const url = hostPath(path) + (query ? '?' + new URLSearchParams(Object.entries(query).filter(([, value]) => value !== '' && value != null)) : '');
    const response = await fetcher(url, {
      method: method || (body ? 'POST' : 'GET'), credentials: 'same-origin', cache: 'no-store', signal,
      headers: {...(body ? {'Content-Type': 'application/json', 'X-Mew-Workbench': '1'} : {}), ...headers},
      ...(body ? {body: JSON.stringify(body)} : {}),
    });
    const data = await response.json();
    const error = data?.error || (typeof data?.detail === 'string' ? data.detail : data?.detail?.message);
    if (!response.ok || error) throw new ApiError(error || VIEW_ERROR, response.status, data);
    return data;
  }
  return {
    readView: async (scope, selection = {}, signal) => alignWorkflowView(await request('/api/workbench/view', {query: {...scope, ...selection}, signal})),
    listTasks: (scope, signal) => request('/api/workbench/tasks', {query: scope, signal}),
    answer: (scope, item, values) => request('/api/workbench/inbox/' + encodeURIComponent(item.id) + '/answer', {
      body: {...values, ...scope, task_id: item.task_id, run_id: item.run_id},
    }),
    prepare: (scope, item, values) => request('/api/workbench/inbox/' + encodeURIComponent(item.id) + '/prepare', {
      body: {...values, ...scope, task_id: item.task_id, run_id: item.run_id},
    }),
    revision: (scope, values) => request('/api/orchestration/task-design/revision', {body: {...values, ...scope}}),
    draft: (scope, values) => request('/api/workbench/revision-drafts', {body: {...values, ...scope}}),
    sources: scope => request('/api/workbench/revision-sources', {query: scope}),
    get: (path, query, signal) => request(path, {query, signal}),
    post: (path, body) => request(path, {body}),
    put: (path, body) => request(path, {body, method: 'PUT'}),
    patch: (path, body, query) => request(path, {body, query, method: 'PATCH'}),
    teamCommand: (run, command, args) => request('/api/team-v2/runs/' + encodeURIComponent(run) + '/commands',
      {body: {command, arguments: args}, headers: {'X-Team-V2': '1'}}),
  };
}
