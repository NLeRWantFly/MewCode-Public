// Adapted from Lens component-patterns.js, MIT © 2026 DayDreamInAReverie.
// Pin, license and changes: NOTICE.md and LICENSE-lens.txt in this directory.
import {node} from '../dom.js';
let patternId = 0;
export function tabs({label, options = [], value, onChange = () => {}}) {
  if (!label?.trim() || !options.length || new Set(options.map(x => x.value)).size !== options.length)
    throw new TypeError('Use an accessible label and unique option values');
  const enabled = options.filter(x => !x.disabled);
  let current = value ?? enabled[0]?.value;
  if (!enabled.some(x => x.value === current)) throw new TypeError('Invalid selection');
  const abort = new AbortController(), signal = abort.signal, id = ++patternId;
  const element = node('div', 'lg-tabs'), bar = node('div', 'lg-segment-bar');
  const indicator = node('span', 'lg-segment-indicator'), entries = [];
  bar.setAttribute('aria-label', label); bar.setAttribute('role', 'tablist');
  indicator.setAttribute('aria-hidden', 'true'); bar.append(indicator); element.append(bar);
  const align = () => {
    const button = entries.find(x => x.option.value === current)?.button;
    if (!button) return;
    indicator.style.width = `${button.offsetWidth}px`;
    indicator.style.setProperty('--lg-segment-x', `${button.offsetLeft}px`);
  };
  for (const option of options) {
    const button = node('button', 'lg-segment-option', option.label), panel = node('div', 'lg-tab-panel');
    button.type = 'button'; button.disabled = Boolean(option.disabled); button.setAttribute('role', 'tab');
    button.id = `wb-tab-${id}-${entries.length}`; panel.id = `wb-panel-${id}-${entries.length}`;
    button.setAttribute('aria-controls', panel.id); panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', button.id); panel.tabIndex = 0;
    if (option.content) panel.append(option.content);
    button.addEventListener('click', () => setValue(option.value, {emit: true}), {signal});
    bar.append(button); element.append(panel); entries.push({option, button, panel});
  }
  function setValue(next, {emit = false} = {}) {
    if (!enabled.some(x => x.value === next)) return false;
    const changed = current !== next; current = next;
    for (const {option, button, panel} of entries) {
      const selected = option.value === current;
      button.tabIndex = selected ? 0 : -1; button.setAttribute('aria-selected', String(selected)); panel.hidden = !selected;
    }
    align(); if (changed && emit) onChange(current); return true;
  }
  bar.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || event.altKey || event.ctrlKey || event.metaKey) return;
    event.preventDefault();
    const index = enabled.findIndex(x => x.value === current);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? enabled.length - 1 :
      (index + (event.key === 'ArrowRight' ? 1 : -1) + enabled.length) % enabled.length;
    setValue(enabled[next].value, {emit: true}); entries.find(x => x.option.value === current).button.focus();
  }, {signal});
  const observer = new ResizeObserver(align); observer.observe(bar); setValue(current);
  return {element, get value() { return current; }, setValue, destroy() { abort.abort(); observer.disconnect(); }};
}
