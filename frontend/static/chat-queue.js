'use strict';

/* Server-authoritative, conversation-scoped follow-up queue.
 *
 * This surface only stages already-uploaded attachments and text. The backend
 * owns ordering and automatic dispatch so reloads and conversation switches do
 * not move a waiting message into the wrong chat.
 */
(() => {
  const HEADER = {'Content-Type': 'application/json', 'X-Mew-Chat-Queue': '1'};
  const queues = new Map();
  const revisions = new Map();
  const pending = new Set();
  let root = null;

  const clean = value => String(value || '').trim();
  const activeConversationId = () => {
    try { return clean(window.MewConversationContext?.getContext?.().conversationId); }
    catch { return ''; }
  };
  const create = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const queueId = () => {
    try {
      if (window.crypto?.randomUUID) return `chat-${window.crypto.randomUUID()}`;
    } catch { /* use bounded fallback */ }
    return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  };
  const normalizeAttachments = value => Array.isArray(value)
    ? value.filter(item => item && typeof item === 'object').map(item => ({...item}))
    : [];
  const normalizeItems = value => Array.isArray(value) ? value.map((item, index) => ({
    id: clean(item?.id),
    text: String(item?.text || ''),
    attachments: normalizeAttachments(item?.attachments),
    position: Number(item?.position) || index + 1,
  })).filter(item => item.id) : [];

  function emitError(message) {
    const detail = {message: clean(message) || '等待区操作失败'};
    window.dispatchEvent(new CustomEvent('mewcode:chat-queue-error', {detail}));
  }

  function sync(payload) {
    const conversationId = clean(payload?.conversation_id || payload?.conversationId);
    if (!conversationId) return false;
    const candidate = Number(payload?.revision);
    const hasRevision = Number.isSafeInteger(candidate) && candidate >= 0;
    const knownRevision = revisions.get(conversationId);
    // HTTP mutations and WebSocket events intentionally race. A delayed HTTP
    // response must never roll the visible queue back after auto-dispatch.
    if (hasRevision && knownRevision !== undefined && candidate < knownRevision) return false;
    if (hasRevision) revisions.set(conversationId, candidate);
    const items = normalizeItems(payload?.items);
    if (items.length) queues.set(conversationId, items);
    else queues.delete(conversationId);
    render();
    return true;
  }

  function syncAll(payload, revisionPayload) {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return;
    const versioned = revisionPayload && typeof revisionPayload === 'object' &&
      !Array.isArray(revisionPayload);
    if (!versioned) {
      queues.clear();
      revisions.clear();
      for (const [conversationId, items] of Object.entries(payload)) {
        const normalized = normalizeItems(items);
        if (clean(conversationId) && normalized.length) queues.set(clean(conversationId), normalized);
      }
      render();
      return;
    }
    const ids = new Set([...Object.keys(payload), ...Object.keys(revisionPayload)]);
    for (const conversationId of ids) {
      sync({
        conversation_id: conversationId,
        items: payload[conversationId] || [],
        revision: revisionPayload[conversationId],
      });
    }
  }

  function itemSummary(item) {
    const text = clean(item.text);
    if (text) return text;
    const count = item.attachments.length;
    return count ? `${count} 个附件` : '空消息';
  }

  function render() {
    if (!root) root = document.getElementById('chat-queue');
    if (!root) return;
    const conversationId = activeConversationId();
    const items = queues.get(conversationId) || [];
    root.replaceChildren();
    root.classList.toggle('hidden', !items.length);
    if (!items.length) return;

    const header = create('div', 'chat-queue-header');
    header.append(
      create('strong', '', `等待发送 · ${items.length}`),
      create('span', '', '当前回复结束后按顺序发送')
    );
    root.appendChild(header);
    const list = create('ol', 'chat-queue-list');
    for (const item of items) {
      const row = create('li', 'chat-queue-item');
      row.dataset.chatQueueId = item.id;
      const position = create('span', 'chat-queue-position', String(item.position));
      position.setAttribute('aria-label', `等待位置 ${item.position}`);
      const copy = create('div', 'chat-queue-copy');
      const summary = create('span', 'chat-queue-text', itemSummary(item));
      summary.title = itemSummary(item);
      copy.appendChild(summary);
      if (item.attachments.length) {
        copy.appendChild(create(
          'small', 'chat-queue-attachments',
          item.attachments.map(attachment => clean(attachment.name) || '附件').join('、')
        ));
      }
      const actions = create('div', 'chat-queue-actions');
      const sendNow = create('button', 'chat-queue-send-now');
      sendNow.type = 'button';
      sendNow.dataset.chatQueueSendNow = item.id;
      sendNow.title = '打断当前回复并立即发送此条';
      sendNow.setAttribute('aria-label', `立即发送第 ${item.position} 条等待消息`);
      sendNow.append(create('span', '', '立即发送'), create('kbd', '', '↵'));
      const remove = create('button', 'chat-queue-remove', '×');
      remove.type = 'button';
      remove.dataset.chatQueueRemove = item.id;
      remove.title = '移出等待区';
      remove.setAttribute('aria-label', `移除第 ${item.position} 条等待消息`);
      const disabled = pending.has(item.id);
      sendNow.disabled = disabled;
      remove.disabled = disabled;
      actions.append(sendNow, remove);
      row.append(position, copy, actions);
      list.appendChild(row);
    }
    root.appendChild(list);
  }

  async function parse(response) {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(clean(payload.error) || `等待区请求失败（${response.status}）`);
      error.code = clean(payload.code);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  async function enqueue({conversationId, text, attachments}) {
    const id = queueId();
    const payload = await parse(await fetch('/api/chat/queue', {
      method: 'POST', credentials: 'same-origin', headers: HEADER,
      body: JSON.stringify({
        conversation_id: conversationId,
        queue_id: id,
        text,
        attachments: normalizeAttachments(attachments).map(item => clean(item.id)).filter(Boolean),
      }),
    }));
    sync(payload);
    return {queueId: id, payload};
  }

  async function mutate(itemId, action) {
    const conversationId = activeConversationId();
    if (!conversationId || pending.has(itemId)) return;
    pending.add(itemId);
    render();
    try {
      const payload = await parse(await fetch(
        `/api/chat/queue/${encodeURIComponent(itemId)}/${action}`,
        {
          method: 'POST', credentials: 'same-origin', headers: HEADER,
          body: JSON.stringify({conversation_id: conversationId}),
        }
      ));
      sync(payload);
    } catch (error) {
      emitError(error.message);
    } finally {
      pending.delete(itemId);
      render();
    }
  }

  function handleClick(event) {
    const sendNow = event.target.closest?.('[data-chat-queue-send-now]');
    if (sendNow) {
      event.preventDefault();
      void mutate(sendNow.dataset.chatQueueSendNow, 'send-now');
      return;
    }
    const remove = event.target.closest?.('[data-chat-queue-remove]');
    if (remove) {
      event.preventDefault();
      void mutate(remove.dataset.chatQueueRemove, 'remove');
    }
  }

  function boot() {
    root = document.getElementById('chat-queue');
    root?.addEventListener('click', handleClick);
    window.addEventListener('mewcode:conversation-context', render);
    render();
  }

  window.MewChatQueue = {
    enqueue,
    sync,
    syncAll,
    render,
    getState: () => ({
      conversationId: activeConversationId(),
      items: normalizeItems(queues.get(activeConversationId()) || []),
      revision: revisions.get(activeConversationId()) || 0,
    }),
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once: true});
  else boot();
})();
