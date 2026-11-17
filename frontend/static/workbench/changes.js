import {node, place} from './dom.js';
import {evidence} from './process.js';
const ACTORS = {user: '你', host: '宿主', coordinator: '协调者', model: '模型'};
export function mountChanges(container) {
  const title = node('h2', 'wb-section-title', '变更记录'); title.id = 'workbench-changes-title';
  const empty = node('p', 'wb-muted', '任务修订与决定会记录在这里'), list = node('ol', 'wb-changes');
  container.replaceChildren(title, empty, list); let owner = ''; const rows = new Map();
  return {update(view) {
    const nextOwner = JSON.stringify([view.task.id, view.task.run_id]);
    if (owner !== nextOwner) { owner = nextOwner; rows.clear(); list.replaceChildren(); }
    empty.hidden = !!view.changes.length; const keep = new Set();
    view.changes.forEach((change, index) => {
      keep.add(change.id); let row = rows.get(change.id); const key = JSON.stringify(change);
      if (!row || row.key !== key) {
        const element = node('li', 'wb-change');
        const actor = change.actor || change.detail?.actor || change.detail?.actor_id;
        element.append(node('strong', '', change.label || change.type || '任务变更'),
          node('p', '', change.reason || change.summary || '已记录'),
          node('p', 'wb-muted', `${ACTORS[actor] || actor || '记录未提供触发者'}${change.time ? ' · ' + change.time : ''}`));
        evidence(element, '查看证据', change.evidence || change.detail || change);
        row?.element.remove(); row = {element, key}; rows.set(change.id, row);
      }
      place(list, row.element, index);
    });
    for (const [id, row] of rows) if (!keep.has(id)) { row.element.remove(); rows.delete(id); }
  }};
}
