// Small deterministic DOM for module behavior; real geometry/focus are checked in the browser.
class Element {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = doc; this.children = []; this.parentNode = null;
    this.attributes = {}; this.dataset = {}; this.listeners = {}; this._text = ''; this.hidden = false;
    this.disabled = false; this.open = false; this.value = ''; this.className = ''; this.style = {setProperty() {}};
    this.classList = {add: name => this.className += ' ' + name, contains: name => this.className.split(' ').includes(name)};
  }
  set textContent(text) { for (const child of this.children) child.parentNode = null; this.children = []; this._text = String(text); }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  get firstChild() { return this.children[0]; }
  get lastChild() { return this.children.at(-1); }
  get isConnected() { return this === this.ownerDocument.body || Boolean(this.parentNode?.isConnected); }
  append(...children) { for (const child of children) this.insertBefore(child, null); }
  appendChild(child) { this.append(child); return child; }
  insertBefore(child, before) {
    child.remove(); child.parentNode = this;
    const index = before ? this.children.indexOf(before) : this.children.length;
    this.children.splice(index, 0, child); return child;
  }
  replaceChildren(...children) { for (const child of this.children) child.parentNode = null; this.children = []; this._text = ''; this.append(...children); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(x => x !== this); this.parentNode = null; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  hasAttribute(key) { return key in this.attributes; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  async dispatch(type, extra = {}) {
    const event = {target: this, preventDefault() {}, ...extra};
    for (const handler of this.listeners[type] || []) await handler(event);
  }
  async click() { if (!this.disabled) await this.dispatch('click'); }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() { this.scrolled = true; }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  querySelectorAll(selector) {
    const matches = element => selector.startsWith('.') ? element.className.split(' ').includes(selector.slice(1)) :
      selector.startsWith('#') ? element.id === selector.slice(1) : selector.split(',').some(tag => element.tagName.toLowerCase() === tag);
    return this.children.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function setup() {
  const document = {createElement: tag => new Element(tag, document)};
  document.createElementNS = (_, tag) => new Element(tag, document);
  document.body = document.createElement('body');
  global.document = document;
  return document;
}
const settle = () => new Promise(resolve => setImmediate(resolve));
module.exports = {setup, settle, Element};
