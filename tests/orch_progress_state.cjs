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
const names = ['expertTrajectoryTime', 'applyExpertTrajectoryEvent', 'hydrateExpertTrajectory'];
const types = source.slice(source.indexOf('  const expertTrajectoryTypes ='), source.indexOf('  function expertTrajectoryTime('));

function fixture() {
  const context = vm.createContext({});
  vm.runInContext(types + names.map(helper).join('\n') + `
    const pipelineStates = new Map(), items = [], renders = [];
    const activeConversationId = () => 'c';
    const pipelineStateFor = id => {
      if (!pipelineStates.has(id)) pipelineStates.set(id, {expertFlows: new Map()});
      return pipelineStates.get(id);
    };
    const noop = () => {};
    const setExpertTimelineFlow = noop, ensureExpertTimelineV2 = noop, syncPipelineClock = noop;
    const pipelineItemFromEvent = (_, item) => items.push(item);
    const renderPipeline = () => renders.push([...pipelineStateFor('c').expertFlows.values()]
      .map(flow => Object.fromEntries(flow.statuses)));
    const flow = {flow_id:'plan', meta:{pipeline_graph_id:'g'},
      nodes:['prepare','implement','verify'].map(node_id => ({node_id})), edges:[]};
    const emit = event => applyExpertTrajectoryEvent({conversation_id:'c', run_id:'r', ...event});
    const current = () => pipelineStateFor('c').expertFlows.get('r');
    const restore = overrides => hydrateExpertTrajectory({run_id:'r', conversation_id:'c',
      locked_flow:flow, last_seq:20, status:'paused', stage_cursor:'implement',
      node_states:{prepare:{status:'completed',passed:true}, implement:{status:'needs_attention'}},
      ...overrides});
  `, context);
  return script => vm.runInContext(script, context);
}

test('same-run flow definitions preserve completion and reject old live events', () => {
  const run = fixture();
  run(`restore(); emit({type:'expert_flow_defined',seq:21,flow});`);
  assert.equal(run(`current().statuses.get('prepare').status`), 'completed');
  assert.equal(run(`current().stageCursor`), 'implement');
  run(`emit({type:'expert_node_started',node_id:'prepare',seq:19});
    emit({type:'expert_flow_defined',seq:20,flow});
    emit({type:'expert_flow_defined',flow});`);
  assert.equal(run(`current().statuses.get('prepare').status`), 'completed');
  assert.equal(run(`current().lastSeq`), 21);
  assert.equal(run(`renders.every(batch => batch[0].prepare?.status === 'completed')`), true);
});

test('historical events fill the timeline without changing checkpoint progress', () => {
  const run = fixture();
  run(`restore(); emit({type:'expert_flow_defined',seq:1,flow,history:true});
    emit({type:'expert_node_completed',node_id:'implement',seq:18,status:'completed',text:'old result',history:true});`);
  assert.equal(run(`current().statuses.get('implement').status`), 'needs_attention');
  assert.equal(run(`current().lastSeq`), 20);
  assert.equal(run(`items.some(item => item.text === 'old result')`), true);
});

test('snapshot replacement is atomic and allows explicit checkpoint invalidation', () => {
  const run = fixture();
  run(`restore(); const previousStates=current().statuses; restore({last_seq:22,stage_cursor:'verify',
    node_states:{prepare:{status:'completed'},implement:{status:'pending'},verify:{status:'paused'}},
    flow_state:{phase:'paused',status:'paused',seq:21}});`);
  assert.equal(run(`current().statuses.get('implement').status`), 'pending');
  assert.equal(run(`current().stageCursor`), 'verify');
  assert.equal(run(`current().status`), 'paused');
  assert.equal(run(`current().lastSeq`), 22);
  assert.equal(run(`current().statuses.size`), 3);
  assert.equal(run(`previousStates === current().statuses`), false);
  assert.equal(run(`previousStates.get('implement').status`), 'needs_attention');
});

test('a delayed snapshot cannot overwrite a newer live checkpoint', () => {
  const run = fixture();
  run(`restore(); emit({type:'expert_node_started',node_id:'verify',seq:23}); restore();`);
  assert.equal(run(`current().statuses.get('verify').status`), 'running');
  assert.equal(run(`current().stageCursor`), 'verify');
  assert.equal(run(`current().status`), 'running');
});

test('a different run starts with its own empty progress', () => {
  const run = fixture();
  run(`restore(); emit({type:'expert_flow_defined',run_id:'new',seq:1,flow});`);
  assert.equal(run(`pipelineStateFor('c').expertFlows.get('new').statuses.size`), 0);
  assert.equal(run(`current().statuses.get('prepare').status`), 'completed');
});

test('an auxiliary repair event never replaces the business step cursor', () => {
  const run = fixture();
  run("restore(); emit({type:'expert_node_started',node_id:'implement__command_probe',seq:21});");
  assert.equal(run("current().stageCursor"), 'implement');
  assert.equal(run("current().statuses.get('prepare').status"), 'completed');
});
