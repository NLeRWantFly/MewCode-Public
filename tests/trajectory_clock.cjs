const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../frontend/static/app.js'), 'utf8').replace(/\r\n/g, '\n');
const helper = name => {
  const start = source.indexOf('  function ' + name + '(');
  assert(start >= 0, name);
  return source.slice(start, source.indexOf('\n  }', start) + 4);
};

function fixture() {
  const context = vm.createContext({});
  vm.runInContext(['pipelineDuration', 'formatPipelineDuration', 'pipelineSpan', 'pipelineTurns',
    'trajectoryTurnDuration', 'tickPipelineClocks', 'syncPipelineClock'].map(helper).join('\n') + `
    let now = 100000, conversationView = 'trajectory', trajectoryDetailTab = 'timing';
    let trajectorySelectedItemId = 'active', pipelineClockTimer = null, callback, intervalCount = 0, cleared = [];
    Date.now = () => now;
    const activeConversationId = () => 'c';
    const items = new Map();
    for (let i = 0; i < 4538; i++) items.set('old' + i, {
      item_id:'old' + i, turn_id:'old-turn', status:'completed', duration_ms:1000,
      started_at:new Date(0).toISOString(), completed_at:new Date(1000).toISOString()});
    items.set('active', {item_id:'active', turn_id:'active-turn', status:'running', started_at:new Date(10000).toISOString()});
    const state = {items, order:[...items.keys()]}, pipelineStates = new Map([['c', state]]);
    const rowTime = {textContent:'before'}, turnTime = {textContent:'before'}, closedTime = {textContent:'closed'};
    const row = {dataset:{itemId:'active'}, querySelector:() => rowTime};
    const wrapper = {dataset:{turnId:'active-turn'}, querySelector:() => turnTime};
    const closedWrapper = {dataset:{turnId:'old-turn'}, querySelector:() => closedTime};
    const detailTime = {dataset:{durationFor:'active'}, textContent:'before'};
    const pipelineItemsEl = {scrollTop:321, focus:row, rows:[row],
      replaceChildren(){throw Error('clock rebuilt history')},
      querySelectorAll(selector){return selector.includes('data-status') ? [row] : [closedWrapper, wrapper]}};
    const trajectoryDetailContentEl = {querySelector:() => detailTime,
      replaceChildren(){throw Error('clock rebuilt selected details')}};
    const renderPipeline = () => {throw Error('clock invoked full render')};
    const setInterval = (fn, interval) => { assertInterval(interval); callback = fn; intervalCount++; return 7; };
    const assertInterval = interval => {if(interval !== 1000)throw Error('interval changed')};
    const clearInterval = id => cleared.push(id);
  `, context);
  return script => vm.runInContext(script, context);
}

test('a live clock updates times without rebuilding 4538 historical rows or changing focus/scroll', () => {
  const run = fixture();
  run('syncPipelineClock(); callback();');
  assert.equal(run('rowTime.textContent'), '1m 30s');
  assert.equal(run('turnTime.textContent'), '1m 30s');
  assert.equal(run('detailTime.textContent'), '1m 30s');
  assert.equal(run('closedTime.textContent'), 'closed');
  assert.equal(run('state.items.size'), 4539);
  assert.equal(run('pipelineItemsEl.focus === row && pipelineItemsEl.rows[0] === row'), true);
  assert.equal(run('pipelineItemsEl.scrollTop'), 321);
  run('now += 5000; callback(); syncPipelineClock();');
  assert.equal(run('rowTime.textContent'), '1m 35s');
  assert.equal(run('intervalCount'), 1);
});

test('the clock stays idle in chat and never updates a different selected detail', () => {
  const run = fixture();
  run('syncPipelineClock(); conversationView = "chat"; callback();');
  assert.equal(run('rowTime.textContent'), 'before');
  run('conversationView = "trajectory"; detailTime.dataset.durationFor = "another"; callback();');
  assert.equal(run('detailTime.textContent'), 'before');
  assert.equal(run('rowTime.textContent'), '1m 30s');
});

test('completion stops the timer and preserves completed durations', () => {
  const run = fixture();
  run('syncPipelineClock(); items.get("active").status = "completed"; syncPipelineClock(); callback();');
  assert.equal(run('pipelineClockTimer'), null);
  assert.equal(run('cleared.length'), 1);
  assert.equal(run('rowTime.textContent'), 'before');
  assert.equal(run('turnTime.textContent'), 'before');
});
