// T93: canvas compatibility entry is now the native workbench, with no bridge.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const read=file=>fs.readFileSync(path.join(__dirname,'../frontend/static',file),'utf8');
const app=read('app.js'),html=read('index.html'),workbench=read('workbench/index.js');
assert.ok(html.includes('id="workbench-sidebar-btn"')&&html.includes('data-open-workbench'));
assert.ok(workbench.includes("querySelectorAll('[data-open-workbench]')"));
assert.ok(workbench.includes("{value: 'structure', label: '结构'"));
assert.ok(workbench.includes('plan.openStep(id)'));
assert.ok(!html.includes('orch-canvas-overlay')&&!app.includes('openOrchCanvasOverlay'));
assert.ok(!app.includes('openCurrentOrchestrationCanvas')&&!app.includes('expertFlowRuntimeChannel'));
console.log('Workbench sidebar, readonly structure and step entry contracts passed');
