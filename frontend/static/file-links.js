/* Local citations stay in the chat; they are never navigated as app routes. */
(function (global) {
  'use strict';
  function localPath(href, relative = false) {
    let value = String(href || '').trim();
    if (!value || value[0] === '#' || /^(?:https?:|mailto:|tel:|data:|javascript:|blob:)/i.test(value)) return null;
    if (/^file:/i.test(value)) {
      try { const url = new URL(value); if (url.hostname && url.hostname !== 'localhost') return null; value = url.pathname; }
      catch { return null; }
    }
    try { value = decodeURIComponent(value); } catch { /* Keep literal percent signs in filenames. */ }
    value = value.replace(/\\/g, '/').replace(/^\/([A-Za-z]:\/)/, '$1');
    if (value.startsWith('//') || /^[A-Za-z][\w+.-]*:/.test(value) && !/^[A-Za-z]:\//.test(value)) return null;
    if (/^\/(?:api|static|workbench|desktop-boot|completion|pipeline|expert-flow|orch-canvas)(?:\/|\?|$)/.test(value)) return null;
    value = value.replace(/(?::\d+(?::\d+)?)$|#L\d+(?:-L?\d+)?$/i, '');
    if (/^[A-Za-z]:\//.test(value) || value.startsWith('/')) return value;
    if (relative && /^(?:\.\.?\/|[^\s]+\.[A-Za-z0-9]{1,12}(?:$|[?#])|[^\s]+\/)/.test(value)) return value;
    return null;
  }
  const escape = value => String(value || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  function renderer(base) {
    const original = base.link.bind(base);
    base.link = function (href, title, text) {
      const path = localPath(href, true);
      return path ? '<a href="#local-file" data-file-reference="' + escape(path) + '" title="' + escape(title || path) + '">' + text + '</a>' : original(href, title, text);
    };
    return base;
  }
  let panel, current = null, sequence = 0;
  const element = (tag, text, className) => { const item = document.createElement(tag); if (text != null) item.textContent = text; if (className) item.className = className; return item; };
  function getPanel() {
    if (panel) return panel;
    const dialog = element('dialog', null, 'mew-file-dialog');
    dialog.setAttribute('aria-label', '引用文件');
    const title = element('strong'), close = element('button', '返回对话'), header = element('header');
    close.type = 'button'; close.addEventListener('click', () => dialog.close()); header.append(title, close);
    const path = element('p', '', 'mew-file-path'), status = element('p', '', 'mew-file-status'); status.setAttribute('role', 'status');
    const preview = element('pre', '', 'mew-file-preview'), actions = element('div', null, 'mew-file-actions');
    const download = element('a', '下载文件'), folder = element('button', '打开所在文件夹'), copy = element('button', '复制路径');
    folder.type = copy.type = 'button'; download.className = 'mew-file-download';
    folder.addEventListener('click', async () => {
      const snapshot = current; if (!snapshot?.directory || !global.mewcodeDesktop?.openDirectory) return;
      try { await global.mewcodeDesktop.openDirectory(snapshot.directory); }
      catch (error) { if (current === snapshot) status.textContent = String(error.message || error); }
    });
    copy.addEventListener('click', async () => {
      const snapshot = current; if (!snapshot) return;
      try { await navigator.clipboard.writeText(snapshot.path); if (current === snapshot) status.textContent = '路径已复制。'; }
      catch { if (current === snapshot) status.textContent = '请选中上方路径复制。'; }
    });
    dialog.addEventListener('close', () => { sequence++; current?.controller.abort(); current = null; });
    dialog.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); dialog.close(); } });
    actions.append(download, folder, copy); dialog.append(header, path, status, actions, preview); document.body.append(dialog);
    panel = {dialog, title, path, status, download, folder, preview}; return panel;
  }
  function context() {
    const value = global.MewWorkbenchHost?.context?.() || {};
    return {conversation_id: value.conversationId || value.conversation_id || '', project: value.project || ''};
  }
  async function open(path) {
    // Native directory opening is already validated by the desktop shell.
    if (global.mewcodeDesktop?.openDirectory && /^(?:[A-Za-z]:\/|\/)/.test(path)) {
      try { await global.mewcodeDesktop.openDirectory(path); return; } catch { /* A file needs the preview API. */ }
    }
    const ui = getPanel(); current?.controller.abort(); const ticket = ++sequence, scope = context();
    const owner = JSON.stringify(scope), controller = new AbortController();
    current = {path, directory: path.replace(/\/[^/]*$/, ''), controller};
    ui.title.textContent = path.split('/').pop() || path; ui.path.textContent = path;
    ui.status.textContent = '正在读取文件…'; ui.preview.textContent = ''; ui.preview.hidden = true;
    ui.download.hidden = true; ui.folder.hidden = !global.mewcodeDesktop?.openDirectory;
    if (!ui.dialog.open) ui.dialog.showModal();
    const valid = () => ticket === sequence && ui.dialog.open && owner === JSON.stringify(context());
    try {
      const query = new URLSearchParams({...scope, path});
      const response = await fetch('/api/file-reference?' + query, {credentials:'same-origin',cache:'no-store',signal:controller.signal});
      const result = await response.json();
      if (!valid()) { if (ticket === sequence) ui.dialog.close(); return; }
      if (!response.ok || result.error) throw new Error(result.error || '当前服务尚未加载文件预览功能，可先打开所在文件夹。');
      current.directory = result.directory; current.path = result.path; ui.path.textContent = result.path;
      ui.status.textContent = result.kind === 'directory' ? '这是一个文件夹。' : result.preview == null ? '此类型暂不支持文本预览，请下载或在文件夹中打开。' : result.truncated ? '预览显示前 512 KiB；下载可查看完整内容。' : '文件已打开。';
      ui.preview.hidden = result.preview == null; ui.preview.textContent = result.preview || '';
      ui.download.hidden = result.kind !== 'file'; ui.download.href = '/api/file-reference?' + new URLSearchParams({...scope,path:result.path,download:'1'});
      ui.download.download = result.name;
    } catch (error) { if (valid() && error.name !== 'AbortError') ui.status.textContent = error.message; }
  }
  function handle(event) {
    const link = event.target.closest?.('a'); if (!link || event.defaultPrevented) return;
    const path = link.dataset.fileReference || localPath(link.getAttribute('href'));
    if (!path) return;
    event.preventDefault(); void open(path);
  }
  if (global.document) {
    document.addEventListener('click', handle);
    document.addEventListener('auxclick', event => { if (event.button === 1) handle(event); });
  }
  global.MewFileLinks = {localPath, renderer, open};
})(window);
