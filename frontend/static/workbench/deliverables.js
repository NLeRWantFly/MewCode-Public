import {node, button, place} from './dom.js';

export async function readDelivery(api, scope, task, item) {
  if (task.mode === 'workflow') return api.get('/api/workbench/artifact', {...scope,
    task_id:task.id,run_id:task.run_id,file_id:item.file_id || item.id});
  if (item.kind === 'inline') return {preview: item.inline_content || '', preview_type: 'text'};
  if (item.kind === 'team_artifact') {
    const value = await api.get('/api/team-v2/artifacts/' + encodeURIComponent(item.artifact_version));
    const bytes = Uint8Array.from(atob(value.content_b64), c => c.charCodeAt(0));
    return {preview: /^(text\/)|json|xml|csv/.test(value.media_type) ? new TextDecoder().decode(bytes) : '二进制文件，请下载查看。', bytes};
  }
  if (!item.file_id) throw new Error('这个历史产物没有可读取的文件快照。');
  const value = await api.get('/api/expert-runs/' + encodeURIComponent(task.run_id) + '/files/' + encodeURIComponent(item.file_id),
    {conversation_id: scope.conversation_id});
  return value.file;
}
export function mountDeliverables(container, {api, store}) {
  const title = node('h2', 'wb-section-title', '交付物'); title.id = 'workbench-deliverables-title';
  const empty = node('p', 'wb-muted', '节点产出的文件会显示在这里'), list = node('div', 'wb-deliverables');
  container.replaceChildren(title, empty, list); const rows = new Map(); let owner = '';
  return {update(view) {
    const nextOwner = JSON.stringify([store.generation, view.task.id, view.task.run_id]);
    if (owner !== nextOwner) { owner = nextOwner; rows.clear(); list.replaceChildren(); }
    const keep = new Set(); empty.hidden = !!view.deliverables.length;
    view.deliverables.forEach((item, index) => {
      keep.add(item.id); let row = rows.get(item.id);
      if (!row) {
        const element = node('article', 'wb-delivery'), name = node('strong'), status = node('span', 'wb-status'), reason = node('p', 'wb-muted');
        const preview = node('div', 'wb-file-preview'); preview.hidden = true;
        const scope = {...store.state.scope}, generation = store.generation, task = view.task;
        row = {element, name, status, reason, item}; const current = row;
        const active = () => generation === store.generation && element.isConnected;
        const show = button('预览文件', async () => {
          if (!preview.hidden) { preview.hidden = true; show.textContent = '预览文件'; return; }
          show.disabled = true; preview.hidden = false; preview.replaceChildren(node('p', 'wb-muted', '正在读取固定版本…'));
          try {
            const file = await readDelivery(api, scope, task, current.item); if (!active()) return;
            if (['image/png', 'image/jpeg'].includes(file.preview_type)) {
              const img = node('img'); img.alt = current.item.name; img.src = 'data:' + file.preview_type + ';base64,' + file.preview;
              img.style.maxWidth = '100%'; preview.replaceChildren(img);
            } else preview.replaceChildren(node('pre', 'wb-evidence-text', file.preview_type === 'none' ? '此格式不支持文本预览。' : file.preview ?? '此格式不支持文本预览。'));
            if (file.preview_truncated) preview.append(node('p', 'wb-muted', '仅显示部分内容。'));
            if (file.bytes || file.content_b64) preview.append(button('下载固定版本', () => {
              const bytes = file.bytes || Uint8Array.from(atob(file.content_b64), c => c.charCodeAt(0));
              const url = URL.createObjectURL(new Blob([bytes], {type: 'application/octet-stream'}));
              const link = node('a'); link.href = url; link.download = current.item.name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 3000);
            }));
            show.textContent = '收起预览';
          } catch (error) { if (active()) { preview.replaceChildren(node('p', 'wb-error', error.message)); show.textContent = '重新预览'; } }
          finally { if (active()) show.disabled = false; }
        });
        element.append(name, status, reason, show, preview); rows.set(item.id, row);
      }
      row.item = item; row.name.textContent = item.name; row.status.textContent = item.status; row.status.dataset.status = item.status;
      row.reason.textContent = item.reason || ''; row.reason.hidden = !row.reason.textContent; place(list, row.element, index);
    });
    for (const [id, row] of rows) if (!keep.has(id)) { row.element.remove(); rows.delete(id); }
  }};
}
