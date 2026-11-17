// Preserve the message being read across reparenting, hiding and history loads.
(() => {
  const element = document.getElementById('messages');
  if (!element) return;
  const positions = new Map();
  let key = '', position = {follow: true, top: 0}, locked = 0, frame = null, expectedTop = null;
  const visible = () => element.clientHeight > 0 && element.getClientRects().length > 0;

  function capture() {
    if (locked || !visible()) return;
    position.top = element.scrollTop;
    if (position.follow) {position.anchor = null; return;}
    const top = element.getBoundingClientRect().top;
    const index = [...element.children].findIndex(child => child.getBoundingClientRect().bottom > top);
    const node = element.children[index];
    position.anchor = node ? {node, index, offset: node.getBoundingClientRect().top - top} : null;
  }

  function apply() {
    if (locked || !visible()) return;
    if (position.follow) element.scrollTop = element.scrollHeight;
    else {
      const anchor = position.anchor;
      const node = anchor && (anchor.node?.parentNode === element ? anchor.node : element.children[anchor.index]);
      element.scrollTop = node
        ? element.scrollTop + node.getBoundingClientRect().top - element.getBoundingClientRect().top - anchor.offset
        : position.top;
    }
    expectedTop = element.scrollTop;
  }

  function schedule() {
    if (frame !== null || locked) return;
    frame = requestAnimationFrame(() => {frame = null; apply();});
  }

  function preserve(change) {
    capture(); locked++;
    try {return change();}
    finally {locked--; apply(); schedule();}
  }

  function select(next) {
    next = String(next || '');
    if (next === key) return;
    capture();
    if (key) positions.set(key, position);
    key = next;
    position = positions.get(key) || {follow: true, top: 0};
    expectedTop = null;
  }

  element.addEventListener('scroll', () => {
    if (locked || !visible()) return;
    if (expectedTop !== null && Math.abs(element.scrollTop - expectedTop) < 1) return;
    expectedTop = null;
    position.follow = element.scrollHeight - element.clientHeight - element.scrollTop < 60;
    capture();
  }, {passive: true});
  // User input takes precedence over an already queued automatic scroll.
  for (const type of ['wheel', 'touchstart', 'pointerdown', 'keydown']) element.addEventListener(type, () => {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null; expectedTop = null;
  }, {passive: true});
  new ResizeObserver(schedule).observe(element);
  new MutationObserver(schedule).observe(element, {childList: true, subtree: true, characterData: true});
  element.addEventListener('load', schedule, true);

  window.MewChatViewport = {
    preserve, select,
    scrollToEnd(force = false) {
      if (locked) return;
      if (force) {position.follow = true; position.anchor = null;}
      if (position.follow) schedule();
    },
    navigate(next, render) {
      if (String(next || '') === key) capture(); else select(next);
      locked++;
      try {return render();}
      finally {locked--; apply(); schedule();}
    },
  };
})();
