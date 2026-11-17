export function createNotices(host, open) {
  const notices = new Map();
  return {
    update(task, items) {
      const owner = JSON.stringify([task.id, task.run_id]), current = new Set(items.map(item => item.id));
      for (const entry of notices.values()) if (entry.owner === owner && !current.has(entry.id)) {
        entry.element.textContent = '这项待处理事项已处理或更新'; entry.element.disabled = true;
      }
      for (const item of items) {
        if (notices.get(item.id)?.element.isConnected) continue;
        const element = host?.appendNotice?.('有 1 件事需要你决定：' + item.title, () => open(item.id, task));
        if (element) notices.set(item.id, {id: item.id, owner, element});
      }
    },
  };
}
