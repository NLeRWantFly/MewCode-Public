import {node, place} from './dom.js';
import {mountStep, isCurrent} from './step.js';
import {mountRequirements} from './revise.js';
export function mountPlan(container, deps) {
  const heading = node('h2', 'wb-section-title', '任务计划'); heading.id = 'workbench-plan-title';
  const list = node('div', 'wb-plan-list'), empty = node('p', 'wb-muted', '计划整理后，步骤会显示在这里。'); container.replaceChildren(heading, empty, list);
  const requirements = node('details', 'wb-plan-requirements'), requirementRows = node('div');
  requirements.append(node('summary', '', '任务要求与决定'), requirementRows); container.insertBefore(requirements, list);
  const requirementsUI = mountRequirements(requirementRows, deps);
  const budget = node('p', 'wb-muted'); container.insertBefore(budget, list);
  const groups = new Map(), steps = new Map(); let owner = '';
  function clear() { for (const entry of steps.values()) entry.destroy(); groups.clear(); steps.clear(); list.replaceChildren(); }
  return {
    update(view) {
      const nextOwner = JSON.stringify([deps.store.generation, view.task.id, view.task.run_id]);
      if (owner !== nextOwner) { owner = nextOwner; clear(); }
      const keepGroups = new Set(), keepSteps = new Set();
      const linked = new Set(view.plan.flatMap(group => group.steps.flatMap(step =>
        [...(step.detail?.constraints || []), ...(step.detail?.requirements || [])].map(row => row.id).filter(Boolean))));
      const unlinked = (view.requirements || []).filter(row => row.kind !== 'goal' && row.kind !== 'method' && !linked.has(row.id));
      requirementsUI.update(unlinked, view.task); requirements.hidden = !unlinked.length;
      empty.hidden = !!view.plan.length;
      budget.hidden = !view.task.budget?.length;
      budget.textContent = (view.task.budget || []).map(row =>
        row.label + '：已用 ' + row.used + '，预留 ' + row.reserved + '，上限 ' + row.limit).join(' · ');
      for (const [groupIndex, group] of view.plan.entries()) {
        keepGroups.add(group.id);
        let item = groups.get(group.id);
        if (!item) {
          const element = node('details', 'wb-milestone'), summary = node('summary', 'wb-milestone-summary');
          const title = node('strong'), status = node('span', 'wb-muted'), content = node('div', 'wb-milestone-steps');
          const deferred = node('div', 'wb-deferred');
          summary.append(title, status); element.append(summary, deferred, content);
          element.open = group.status !== '已完成';
          item = {element, title, status, content, deferred, active: false}; groups.set(group.id, item);
        }
        const active = group.steps.some(isCurrent);
        if (active && !item.active) item.element.open = true;
        item.active = active; item.title.textContent = group.title; item.status.textContent = group.status;
        item.deferred.hidden = !group.deferred; item.deferred.replaceChildren();
        if (group.deferred) {
          item.deferred.append(node('p', 'wb-muted', '前序交付验收后，再生成本里程碑的具体步骤。'));
          for (const check of group.checks || []) for (const criterion of check.criteria || [])
            item.deferred.append(node('p', '', '验收要求：' + criterion));
        }
        for (const [stepIndex, step] of group.steps.entries()) {
          keepSteps.add(step.id);
          let entry = steps.get(step.id);
          if (!entry) { entry = mountStep(step, {...deps, task: view.task}); steps.set(step.id, entry); }
          entry.update(step, view.task, view.requirements || []);
          place(item.content, entry.element, stepIndex);
        }
        place(list, item.element, groupIndex);
      }
      for (const [id, entry] of steps) if (!keepSteps.has(id)) { entry.destroy(); steps.delete(id); }
      for (const [id, entry] of groups) if (!keepGroups.has(id)) { entry.element.remove(); groups.delete(id); }
    },
    openStep(id) {
      const step = steps.get(id); if (!step) return false;
      for (const group of groups.values()) if (group.content.contains(step.element)) group.element.open = true;
      step.open(); return true;
    },
    destroy: clear,
  };
}
