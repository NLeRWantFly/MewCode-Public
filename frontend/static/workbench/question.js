import {node, button} from './dom.js';

export function inputValue(text, schema = {}) {
  const value = text.trim();
  if (['list', 'multiselect'].includes(schema.input_type || schema.type)) return value.split(schema.separator || '\n').map(x => x.trim()).filter(Boolean);
  if (['number', 'integer'].includes(schema.type)) {
    if (!value || !Number.isFinite(Number(value)) || (schema.type === 'integer' && !Number.isInteger(Number(value)))) throw new Error('请填写有效数值');
    return Number(value);
  }
  return value;
}

// Native inputs provide keyboard selection. Canonical values never come from display labels.
export function mountQuestion(question, {bundle = false, recommended, index = 0, prefix = ''} = {}) {
  const field = node('fieldset', 'wb-question'), legend = node('legend', '', question.question || question.message || question.label || '请补充信息');
  field.append(legend);
  if (question.context || question.description) field.append(node('p', 'wb-muted', question.context || question.description));
  const name = question.name || 'q' + index, multiple = !!(question.multiple || question.multi_select || question.multiSelect || ['multiselect', 'checkbox'].includes(question.type));
  const prepared = question.prepared_choices || [];
  const options = (question.options || question.choices || []).map(raw => {
    const option = typeof raw === 'object' ? raw : {value: raw, label: String(raw)};
    const value = option.value ?? option.id;
    return {...option, ...prepared.find(row => row.id === value), label: option.label, description: option.description, value};
  });
  if (!prepared.length && (question.allow_other || question.allowOther)) options.push({value: '__other__', label: '其他，自己填写', manual: true, requires_input: true});
  const chosen = new Map(), inputs = new Map();
  if (question.details) { const details = node('details', 'wb-answer-notes'); details.append(node('summary', '', '查看背景详情'), node('p', 'wb-muted', question.details)); field.append(details); }
  const free = node('textarea', 'wb-input'); free.rows = 3; free.maxLength = question.max_length || 16000;
  free.setAttribute('aria-label', question.input_label || legend.textContent);
  let page = 0;
  const list = node('div', 'wb-options'), rows = [];
  const pages = node('div', 'wb-option-pages');
  const pageText = node('span', 'wb-meta');
  const previous = button('上一组选项', () => paginate(page - 1)), next = button('下一组选项', () => paginate(page + 1));
  function paginate(value) {
    page = Math.max(0, Math.min(Math.ceil(rows.length / 4) - 1, value));
    rows.forEach((row, i) => { row.hidden = Math.floor(i / 4) !== page; });
    pageText.textContent = `${page + 1} / ${Math.ceil(rows.length / 4)}`;
    previous.disabled = page === 0; next.disabled = (page + 1) * 4 >= rows.length;
  }
  const controls = [];
  if (!options.length || question.input_mode === 'text') {
    if (['number', 'integer'].includes(question.type)) {
      const number = node('input', 'wb-input'); number.type = 'number'; number.step = question.type === 'integer' ? '1' : 'any';
      number.setAttribute('aria-label', legend.textContent);
      if (question.min != null) number.min = question.min; if (question.max != null) number.max = question.max;
      inputs.set('free', number); controls.push(number); field.append(number);
    } else { inputs.set('free', free); controls.push(free); field.append(free); }
  } else {
    for (const [i, option] of options.entries()) {
      const row = node('div', 'wb-option'), label = node('label', 'wb-choice-label');
      const input = node('input'); input.type = multiple ? 'checkbox' : 'radio'; input.name = prefix + '-' + name; input.value = String(i);
      const text = node('span', 'wb-choice-text', option.label || '选项 ' + (i + 1));
      if (option.recommended || JSON.stringify(recommended) === JSON.stringify(option.value)) text.append(node('small', 'wb-recommended', '推荐'));
      label.append(input, text); row.append(label); controls.push(input);
      if (option.description || option.consequence) row.append(node('p', 'wb-choice-effect', option.consequence || option.description));
      if (option.requires_input || option.manual || option.action === 'manual') {
        const extra = node('textarea', 'wb-input'); extra.rows = 2; extra.hidden = true;
        extra.setAttribute('aria-label', option.input_label || question.input_label || '补充这项选择'); extra.maxLength = 16000;
        inputs.set(i, extra); controls.push(extra); row.append(extra);
      }
      input.addEventListener('change', () => {
        if (!multiple) { chosen.clear(); for (const other of controls) if (other.type === 'radio' && other !== input) other.checked = false; }
        if (input.checked) chosen.set(i, option); else chosen.delete(i);
        for (const [key, extra] of inputs) extra.hidden = !chosen.has(key);
      });
      rows.push(row); list.append(row);
    }
    field.append(list);
    if (rows.length > 4) { pages.append(previous, pageText, next); field.append(pages); paginate(0); }
  }
  const note = node('textarea', 'wb-input wb-question-note'); note.rows = 2; note.setAttribute('aria-label', '这项回答的备注');
  const notes = node('details', 'wb-answer-notes'); notes.append(node('summary', '', '补充备注（可选）'), note); field.append(notes); controls.push(note);
  let visible = true;
  function read({partial = false} = {}) {
    const result = {name, note: note.value.trim(), choiceInputs: {}, actions: []}, direct = inputs.get('free');
    if (!visible) return {...result, note: ''};
    if (direct) {
      const text = direct.value.trim();
      if (!text) { if (!partial && question.required !== false) throw new Error('请填写：' + legend.textContent); return result; }
      const value = inputValue(text, question);
      if (typeof value === 'number' && ((question.min != null && value < question.min) || (question.max != null && value > question.max))) throw new Error('数值超出允许范围');
      result.value = bundle ? {custom: value} : value; return result;
    }
    if (!chosen.size) { if (!partial && question.required !== false) throw new Error('请选择：' + legend.textContent); return result; }
    const values = [];
    for (const [key, option] of chosen) {
      const extra = inputs.get(key), text = extra?.value.trim() || '';
      if (option.requires_input && !text) throw new Error(option.input_label || '请填写所选项的补充信息');
      const value = extra ? inputValue(text, question) : undefined;
      if (option.kind === 'action' && option.action !== 'manual') { result.actions.push({action: option.action, input: value, name, token: option.value}); continue; }
      if (option.manual || option.action === 'manual') {
        if (!text) throw new Error('请填写自己的回答');
        if (question.preserve_choice_value && !bundle) {
          values.push(option.value); result.choiceInputs[option.value] = value; continue;
        }
        if (bundle) {
          if (chosen.size !== 1) throw new Error('自行填写时，请取消其他选择并在输入中填写完整答案');
          result.value = {custom: value}; return result;
        }
        values.push(value);
      } else { values.push(option.value); if (value !== undefined) result.choiceInputs[option.value] = value; }
    }
    if (values.length) result.value = multiple ? (!bundle && (question.type === 'checkbox' || question.multiSelect) ? values.flat().join(', ') : values.flat()) : values[0];
    return result;
  }
  return {element: field, name, condition: question.required_when, read,
    value() {
      try { const value = read({partial: true}).value;
        if (value?.custom !== undefined) return value.custom;
        if (Array.isArray(value)) return value.map(token => prepared.find(row => row.id === token)?.value ?? token);
        return prepared.find(row => row.id === value)?.value ?? value;
      } catch { return undefined; }
    },
    show(value) { visible = value; field.hidden = !value; },
    disable(value) { field.disabled = value; for (const control of controls) control.disabled = value; }};
}
