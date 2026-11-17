import {node, button} from './dom.js';
export const ENTRY_NAMES = {goal:'目标', milestone:'里程碑', node:'步骤与指定方法', constraint:'约束', acceptance:'验收标准',
  artifact:'交付要求', decision:'决定', assumption:'假设', question:'待确认的问题', input:'运行参数'};
const EVIDENCE = {user_source:'关联用户原话 · 归纳待核对', reported:'历史汇报 · 尚未验收',
  proposal:'助手建议 · 尚未确认', saved_graph:'已有流程定义'};

export function mountDesignSources(container, {api, store}) {
  let owner = '', signature = '';
  return {update(entries, task) {
    const nextOwner = JSON.stringify([store.generation, task?.id]);
    const nextSignature = JSON.stringify(entries);
    if (owner === nextOwner && signature === nextSignature) return;
    const open = new Set(owner === nextOwner ? [...container.querySelectorAll('details')].filter(el=>el.open).map(el=>el.dataset.key) : []);
    owner = nextOwner; signature = nextSignature;
    const generation = store.generation, scope = {...store.state.scope};
    container.replaceChildren();
    for (const entry of entries.filter(row=>ENTRY_NAMES[row.kind])) {
      const details = node('details', 'wb-design-entry'); details.dataset.key = entry.id; details.open = open.has(entry.id);
      details.append(node('summary', '', `${ENTRY_NAMES[entry.kind]} · ${entry.title || entry.body || '正在整理'}`), node('p', '', entry.body));
      if (entry.data?.acceptance) details.append(node('p', 'wb-muted', '验收条件：' + [].concat(entry.data.acceptance).join('；')));
      details.append(node('p', 'wb-muted', entry.reviewed ? '已人工核对 · 来源保留备查' : EVIDENCE[entry.evidence] || '任务规格'));
      for (const ref of entry.sources || []) {
        const source = node('div', 'wb-design-source'); source.append(node('blockquote', '', ref.quote));
        if (ref.source_id) {
          const original = node('pre', 'wb-evidence'); original.hidden = true;
          const read = button('查看原话', async () => {
            if (!original.hidden) { original.hidden = true; return; }
            const restoreFocus = document.activeElement === read;
            read.disabled = true;
            try {
              const value = await api.get('/api/orchestration/draft/source/' + encodeURIComponent(ref.source_id), scope);
              if (generation !== store.generation || owner !== nextOwner || !source.isConnected) return;
              original.textContent = value.text || ref.quote || ''; original.hidden = false;
            } catch (error) { if (generation === store.generation && source.isConnected) {original.textContent = error.message; original.hidden = false;} }
            finally { read.disabled = false; if (restoreFocus && generation === store.generation && source.isConnected && document.activeElement === document.body) read.focus(); }
          });
          source.append(read, original);
        }
        details.append(source);
      }
      container.append(details);
    }
  }};
}
