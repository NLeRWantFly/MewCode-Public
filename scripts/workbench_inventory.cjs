'use strict';
// Recount named app.js functions using Node's bundled Acorn, without npm/build.
const fs = require('node:fs');
const vm = require('node:vm');
const parser = {};
vm.runInNewContext(process.binding('natives')['internal/deps/acorn/acorn/dist/acorn'],
  {exports: parser, module: {exports: parser}});
const source = fs.readFileSync('frontend/static/app.js', 'utf8');
const tree = parser.parse(source, {ecmaVersion: 'latest', locations: true});
const functions = [];
function walk(node, depth = 0) {
  if (!node || typeof node !== 'object') return;
  if (node.type === 'FunctionDeclaration') functions.push({
    name: node.id.name, start: node.loc.start.line, end: node.loc.end.line, depth,
  });
  const next = depth + (/Function/.test(node.type || '') ? 1 : 0);
  for (const [key, value] of Object.entries(node)) {
    if (key === 'loc') continue;
    if (Array.isArray(value)) value.forEach(child => walk(child, next));
    else if (value && typeof value === 'object') walk(value, next);
  }
}
walk(tree);
process.stdout.write(JSON.stringify(functions));
