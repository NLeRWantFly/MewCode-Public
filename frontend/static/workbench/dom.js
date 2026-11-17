// All host text is inserted as text, including evidence and model output.
export function node(tag, className = '', text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = String(text);
  return element;
}
export function button(label, action, className = 'wb-button') {
  const element = node('button', className, label);
  element.type = 'button';
  element.addEventListener('click', action);
  return element;
}
export function icon(paths) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
  for (const [key,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.6','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true'})) svg.setAttribute(key,value);
  for (const d of paths) {
    const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',d);svg.append(path);
  }
  return svg;
}
export function place(parent, child, index) {
  if (parent.children[index] !== child) parent.insertBefore(child, parent.children[index] || null);
}
export function section(id, title, subtitle = '') {
  const element = node('section', 'wb-section');
  element.id = 'workbench-' + id;
  const heading = node('h2', 'wb-section-title', title);
  element.setAttribute('aria-labelledby', heading.id = element.id + '-title');
  element.append(heading);
  if (subtitle) element.append(node('p', 'wb-muted', subtitle));
  return element;
}
