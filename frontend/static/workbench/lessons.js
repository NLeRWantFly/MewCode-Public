import {node, button} from './dom.js';
export function mountLessons(container, {api, flowId, stepId, active, onChanged = () => {}}) {
  const status = node('p', 'wb-muted'), list = node('div'), form = node('form', 'wb-form');
  container.replaceChildren(node('p', 'wb-muted', '经验可用于同类任务，不能覆盖当前要求与授权。待确认的经验请在“待你处理”中决定。'), status, list, form);
  let revision = 0, pending = false;
  const endpoint = '/api/expert/lessons?flow_id=' + encodeURIComponent(flowId);
  const input = node('textarea'); input.required = true; input.maxLength = 2000; input.rows = 3; input.setAttribute('aria-label', '经验内容');
  input.placeholder = '记录下次可以复用的做法';
  const scope = node('select'); scope.setAttribute('aria-label', '经验范围');
  for (const [value, label] of [[stepId, '当前步骤'], ['*', '整个流程']]) {
    const option = node('option', '', label); option.value = value; scope.append(option);
  }
  const submit = node('button', 'wb-button', '添加并启用'); submit.type = 'submit';
  form.append(input, scope, submit);
  async function mutate(body, add = false, flow = flowId) {
    if (!active() || pending) return;
    pending = true; for (const control of container.querySelectorAll('button')) control.disabled = true;
    try {
      const url = '/api/expert/lessons?flow_id=' + encodeURIComponent(flow);
      if (add) await api.post(url, body); else await api.patch(url, body);
      if (!active()) return;
      if (add) input.value = '';
      onChanged(); await read();
    } catch (error) { if (active()) status.textContent = error.message; }
    finally { pending = false; if (active()) for (const control of container.querySelectorAll('button')) control.disabled = false; }
  }
  form.addEventListener('submit', event => {
    event.preventDefault(); if (input.value.trim()) void mutate({scope: scope.value, text: input.value.trim()}, true);
  });
  async function read() {
    const serial = ++revision; status.textContent = '正在读取经验…';
    try {
      const data = await api.get(endpoint);
      if (!active() || serial !== revision) return;
      const rows = (data.lessons || []).filter(row => row.status !== 'pending' && ['*', stepId].includes(row.scope));
      status.textContent = rows.length ? `${rows.length} 条已记录经验` : '当前步骤暂无已采用的经验。';
      list.replaceChildren(...rows.map(row => {
        const item = node('section', 'wb-lesson');
        item.append(node('p', '', row.text), node('small', 'wb-muted', `${row.status === 'active' ? '已启用' : '已停用'} · 成功 ${row.success_hits || 0} / 使用 ${row.hits || 0}`));
        if (row.why) item.append(node('p', 'wb-muted', row.why));
        const actions = node('div', 'wb-actions');
        actions.append(button(row.status === 'active' ? '停用' : '启用', () => void mutate({lesson_id: row.lesson_id, status: row.status === 'active' ? 'disabled' : 'active'}, false, row.flow_id)),
          button('删除', () => {
            actions.replaceChildren(node('span', 'wb-muted', '删除这条经验？'),
              button('确认删除', () => void mutate({lesson_id: row.lesson_id, delete: true}, false, row.flow_id), 'wb-button wb-danger'),
              button('保留', () => void read()));
          }));
        item.append(actions); return item;
      }));
    } catch (error) { if (active()) status.textContent = error.message; }
  }
  void read(); return {refresh: read, destroy() { revision++; }};
}
