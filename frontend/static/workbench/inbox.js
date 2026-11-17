import {node, place} from './dom.js';
import {mountDecisionCard} from './decision-card.js';

function cardSignature(item) {
  // An unchanged decision keeps its draft; the same request can become live
  // or receive different questions without changing its host-owned id.
  return JSON.stringify(item, (_key, value) => value && typeof value === 'object' && !Array.isArray(value)
    ? Object.fromEntries(Object.keys(value).sort().map(key => [key, value[key]])) : value);
}

export function mountInbox(container, deps) {
  const heading = node('h2', 'wb-section-title', '待你处理'); heading.id = 'workbench-inbox-title';
  heading.tabIndex = -1;
  const conflict = node('p', 'wb-error wb-inbox-conflict'); conflict.setAttribute('role', 'alert'); conflict.hidden = true;
  const empty = node('p', 'wb-muted', '当前没有待处理事项'), list = node('div', 'wb-inbox-list'); container.replaceChildren(heading, conflict, empty, list);
  const cards = new Map(); let owner = '', currentTask = null;
  function clearConflict() { conflict.textContent = ''; conflict.hidden = true; }
  function clear() { for (const card of cards.values()) card.destroy(); cards.clear(); clearConflict(); }
  return {
    update(view) {
      const key = JSON.stringify([deps.store.generation, view?.task.id, view?.task.run_id]);
      if (owner !== key) { clear(); owner = key; }
      if (!view) return;
      currentTask = view.task;
      const keep = new Set((view.inbox || []).map(item => item.id));
      let restoreFocus = false, replacementFocus = null;
      for (const [id, card] of cards) if (!keep.has(id)) {
        restoreFocus ||= card.element.contains(document.activeElement) || card.shouldRestoreFocus(); card.destroy(); cards.delete(id);
      }
      empty.hidden = keep.size > 0; heading.textContent = keep.size ? `待你处理 · ${keep.size}` : '待你处理';
      for (const [index, item] of (view.inbox || []).entries()) {
        let card = cards.get(item.id);
        const signature = cardSignature(item);
        const replacing = card && card.signature !== signature;
        const focusReplacement = replacing && (card.element.contains(document.activeElement) || card.shouldRestoreFocus());
        if (replacing) { card.destroy(); cards.delete(item.id); card = null; }
        if (!card) { card = mountDecisionCard(item, {...deps,
          onConflict: message => { conflict.textContent = message; conflict.hidden = false; },
          resolved: id => { clearConflict(); deps.resolved?.(id); }}); card.signature = signature; cards.set(item.id, card); }
        place(list, card.element, index);
        if (focusReplacement) replacementFocus = card.element;
      }
      deps.notices?.update(view.task, view.inbox || []);
      if (replacementFocus) replacementFocus.focus();
      else if (restoreFocus) (cards.values().next().value?.element || heading).focus();
    },
    open(id) { const card = cards.get(id); if (!card) return false; card.focus(); return true; },
    get task() { return currentTask; }, destroy: clear,
  };
}
