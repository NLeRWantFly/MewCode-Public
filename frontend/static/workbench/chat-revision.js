import {node, button} from './dom.js';

// Match original raw text and occurrence, never rendered Markdown or model text.
export function sourceForMessage(messages, sources, target) {
  const rendered = messages.filter(row => row.text === target.text);
  const originals = sources.filter(row => row.role === 'user' && row.text === target.text);
  if (rendered.length !== originals.length || !originals.length) throw new Error('消息尚未保存或来源不完整，请刷新后重试。');
  return originals[rendered.indexOf(target)];
}
export function mountChatRevision({api, store, host, openInbox}) {
  const actions = new Map(); let current = null;
  function update(view) {
    current = view;
    const enabled = view?.task.mode === 'collaborative' && ['进行中', '已暂停', '等你处理'].includes(view.task.status)
      && !view.task.readonly && view.task.revision_available !== false;
    const messages = host?.userMessages?.() || [];
    for (const [element, action] of actions) if (!element.isConnected) { action.remove(); actions.delete(element); }
    for (const message of messages) {
      let action = actions.get(message.element);
      if (!action) {
        const status = node('span', 'wb-chat-revision-status'); status.setAttribute('role', 'status');
        const link = button('作为要求修改', async () => {
          const task = current?.task, generation = store.generation, scope = {...store.state.scope};
          if (!task || link.disabled) return;
          const active = () => generation === store.generation && message.element.isConnected;
          link.disabled = true; status.textContent = '正在整理修订草稿…';
          try {
            const rows = host.userMessages(), target = rows.find(row => row.element === message.element);
            const sources = await api.sources({...scope, task_id: task.id, run_id: task.run_id});
            if (!active()) return;
            const source = sourceForMessage(rows, sources.messages, target);
            const result = await api.draft(scope, {task_id: task.id, run_id: task.run_id, message_id: source.id});
            if (!active()) return;
            status.textContent = result.draft.status === 'pending' ? '草稿已整理，请在待你处理中确认。'
              : result.draft.message_text || result.draft.error || (result.draft.status === 'accepted' ? '这份修订草稿已采用。' : '这条消息的修订处理已记录。');
            store.refresh(); if (result.draft.status === 'pending') openInbox();
          } catch (error) { if (active()) status.textContent = error.message; }
          finally { if (active()) link.disabled = false; }
        }, 'wb-chat-revision-button');
        action = node('div', 'wb-chat-revision'); action.append(link, status); message.element.append(action); actions.set(message.element, action);
      }
      action.hidden = !enabled;
    }
  }
  const abort = new AbortController();
  window.addEventListener('mewcode:user-message', () => update(current), {signal: abort.signal});
  return {update, destroy() { abort.abort(); for (const action of actions.values()) action.remove(); actions.clear(); }};
}
