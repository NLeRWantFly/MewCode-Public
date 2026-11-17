const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const path = require('node:path');
const load = name => import(pathToFileURL(path.resolve(__dirname, '../frontend/static/workbench/' + name)));

function clock() {
  let at = 0, serial = 0;
  const timers = new Map(), frames = new Map();
  return {
    now: () => at,
    delay(fn, ms) { const id = ++serial; timers.set(id, {at: at + ms, fn}); return id; },
    cancelDelay: id => timers.delete(id),
    frame(fn) { const id = ++serial; frames.set(id, fn); return id; },
    cancelFrame: id => frames.delete(id),
    paint() { const pending = [...frames.values()]; frames.clear(); pending.forEach(fn => fn()); },
    async tick(ms = 0) {
      const end = at + ms;
      for (;;) {
        const next = [...timers].filter(([, x]) => x.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        at = next[1].at; timers.delete(next[0]); next[1].fn();
        for (let n = 0; n < 8; n++) await Promise.resolve();
      }
      at = end;
      for (let n = 0; n < 8; n++) await Promise.resolve();
    },
  };
}
const view = (version, id = 'a', cid = 'conversation') => ({version, task: {id, run_id: id, conversation_id: cid}});
const scope = {conversation_id: 'conversation', project: 'p'};
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };

(async () => {
  const {createStore} = await load('store.js'), {createAPI, VIEW_ERROR} = await load('api.js');
  const {mountLayout} = await load('layout.js');
  let cases = 0;
  {
    const c = clock(), history = deferred(), stale = deferred(); let calls = 0, lists = 0;
    const at = (rev, status) => ({...view(String(rev)), task: {...view('').task, kernel_revision: rev, status}});
    const store = createStore({api: {readView: async () => ++calls === 1 ? at(4, '进行中') : stale.promise,
      listTasks: () => { lists++; return history.promise; }}, ...c});
    store.setScope(scope); await c.tick(); c.paint();
    assert.equal(store.state.view.task.status, '进行中', 'History lookup must not delay the active run');
    store.refresh({tasks:true}); await c.tick(250);
    assert.equal(lists, 1, 'Slow history reads must coalesce');
    store.accept(at(6, '已暂停')); stale.resolve(at(5, '进行中')); await c.tick();
    assert.equal(store.state.view.task.status, '已暂停', 'Older read must not replace newer control response');
    history.resolve({tasks: [{...view('').task, kernel_revision: 3, status: '等待开始'}]}); await c.tick();
    assert.equal(store.state.tasks[0].status, '已暂停');
    store.dispose(); cases++;
  }
  {
    const c = clock(), old = {id:'history',run_id:'old',status:'已停止',readonly:true};
    const store = createStore({api:{readView:async()=>({...view('1'),task:{...view('1').task,status:'进行中'}}),
      listTasks:async()=>({tasks:[{id:'a',run_id:'a',status:'进行中'},old]})},...c});
    store.setScope(scope);await c.tick();c.paint();
    store.accept({...view('2'),task:{...view('2').task,status:'已完成',title:'新的标题'}});c.paint();
    assert.equal(store.state.tasks[0].status,'已完成');assert.equal(store.state.tasks[0].title,'新的标题');
    assert.deepEqual(store.state.tasks[1],old);store.dispose();cases++;
  }
  {
    const c = clock(), store = createStore({api: {}, ...c}); let renders = 0;
    store.subscribe(() => renders++);
    for (let i = 0; i < 10; i++) { store.accept(view('same', 'a', '')); c.paint(); }
    assert.equal(renders, 1);
    for (let i = 0; i < 10; i++) store.accept(view(String(i), 'a', ''));
    c.paint(); assert.equal(renders, 2); assert.equal(store.state.view.version, '9');
    store.dispose(); cases++;
  }
  {
    const c = clock(), reads = [], rendered = []; let version = 0;
    const store = createStore({api: {
      readView: async () => { reads.push(c.now()); return view(String(version)); },
      listTasks: async () => ({tasks: [{id: 'a', run_id: 'a'}]}),
    }, ...c});
    store.subscribe(state => { if (state.view) rendered.push(c.now()); });
    store.setScope(scope); await c.tick(); c.paint();
    for (let i = 0; i < 100; i++) {
      version++;
      store.changed({conversation_id: scope.conversation_id, task_id: 'a', run_id: 'a', version: String(version)});
      await c.tick(10); c.paint();
    }
    await c.tick(250); c.paint();
    for (const time of reads) assert.ok(reads.filter(x => x >= time && x < time + 1000).length <= 4, reads);
    for (const time of rendered) assert.ok(rendered.filter(x => x >= time && x < time + 1000).length <= 4, rendered);
    assert.equal(store.state.view.version, '100');
    assert.ok(reads.length <= 5);
    const before = reads.length;
    store.changed({conversation_id: scope.conversation_id, task_id: 'a', run_id: 'a', version: '100'});
    await c.tick(1000); assert.equal(reads.length, before);
    // State after reconnect must read even when the cached version matches.
    version = 101; store.reconnect(scope); await c.tick(); c.paint();
    assert.equal(store.state.view.version, '101'); assert.equal(reads.length, before + 1);
    store.dispose(); cases++;
  }
  {
    const c = clock(), old = deferred(), fresh = deferred(), selected = [];
    let call = 0;
    const store = createStore({api: {
      readView: async (_scope, task) => { selected.push(task); return ++call === 1 ? old.promise : fresh.promise; },
      listTasks: async () => ({tasks: [{id: 'a', run_id: 'a'}, {id: 'b', run_id: 'b'}]}),
    }, ...c});
    store.setScope(scope); await c.tick();
    store.selectTask({id: 'b', run_id: 'b'}); await c.tick(250);
    fresh.resolve(view('new', 'b')); await c.tick(); c.paint();
    old.resolve(view('stale', 'a')); await c.tick(); c.paint();
    assert.equal(store.state.view.task.id, 'b'); assert.equal(store.state.view.version, 'new');
    assert.deepEqual(selected[1], {task_id: 'b', run_id: 'b'});
    assert.equal(store.accept(view('foreign', 'b', 'other')), false);
    store.dispose(); cases++;
  }
  {
    const c = clock(), old = deferred(); let n = 0;
    const store = createStore({api: {readView: async () => ++n === 1 ? old.promise : null,
      listTasks: async () => ({tasks: []})}, ...c});
    store.setScope(scope); await c.tick(); store.setScope({...scope, conversation_id: 'other'});
    await c.tick(250); old.resolve(view('old')); await c.tick();
    assert.equal(store.state.view, null); assert.equal(store.state.scope.conversation_id, 'other');
    store.dispose(); cases++;
  }
  {
    const c = clock(), store = createStore({api: {readView: async () => { throw new Error('internal stack'); },
      listTasks: async () => ({tasks: []})}, ...c});
    store.setScope(scope); await c.tick(); c.paint();
    assert.equal(store.state.error, VIEW_ERROR); assert.equal(store.state.loading, false);
    store.dispose(); cases++;
  }
  {
    const calls = [], api = createAPI(async (url, options) => {
      calls.push({url, options}); return {ok: true, json: async () => ({ok: true})};
    });
    await api.answer(scope, {id: 'item/key', task_id: 'a', run_id: 'a'}, {task_id: 'evil', conversation_id: 'wrong', value: 'yes'});
    assert.equal(calls[0].url, '/api/workbench/inbox/item%2Fkey/answer');
    const body = JSON.parse(calls[0].options.body);
    assert.equal(body.task_id, 'a'); assert.equal(body.conversation_id, scope.conversation_id);
    assert.equal(calls[0].options.headers['X-Mew-Workbench'], '1');
    await api.readView(scope, {task_id: 'a'});
    assert.equal(calls[1].options.cache, 'no-store'); assert.match(calls[1].url, /task_id=a/);
    const rejected = createAPI(async () => ({ok: false, status: 409, json: async () => ({error: '已处理'})}));
    await assert.rejects(rejected.get('/test'), error => error.status === 409 && error.message === '已处理'); cases++;
  }
  {
    const {setup}=require('./workbench_dom.cjs'),doc=setup();doc.getElementById=id=>doc.body.querySelector('#'+id);
    const background=['conversation-pane','topbar','drawer'].map(id=>{const element=doc.createElement('div');element.id=id;element.inert=id==='drawer';return element;});
    const aside=doc.createElement('aside'),toggle=doc.createElement('button'),close=doc.createElement('button'),separator=doc.createElement('div'),backdrop=doc.createElement('div'),win=doc.createElement('div');
    aside.append(close);doc.body.append(...background,toggle,aside,separator,backdrop);toggle.focus();
    const layout=mountLayout({aside,toggle,close,separator,backdrop,window:win});assert.equal(aside.hidden,true);
    layout.open();assert.equal(layout.isOpen(),true);assert.ok(background.every(element=>element.inert));assert.equal(doc.activeElement,close);assert.equal(separator.hidden,true);
    await win.dispatch('keydown',{key:'Escape',defaultPrevented:true});assert.equal(layout.isOpen(),true);
    await win.dispatch('keydown',{key:'Escape'});assert.equal(layout.isOpen(),false);assert.deepEqual(background.map(element=>element.inert),[false,false,true]);assert.equal(doc.activeElement,toggle);
    await toggle.click();assert.equal(layout.isOpen(),true);layout.destroy();assert.deepEqual(background.map(element=>element.inert),[false,false,true]);cases++;
  }
  {
    const {setup}=require('./workbench_dom.cjs'),doc=setup();doc.getElementById=id=>doc.body.querySelector('#'+id);
    const home=doc.createElement('div'),conversation=doc.createElement('div'),input=doc.createElement('textarea');conversation.id='conversation-pane';input.value='保留未发送的要求';conversation.append(input);home.append(conversation);doc.body.append(home);
    const aside=doc.createElement('aside'),chatSlot=doc.createElement('section'),toggle=doc.createElement('button'),close=doc.createElement('button');aside.append(close,chatSlot);doc.body.append(toggle,aside);
    const layout=mountLayout({aside,toggle,close,chatSlot,separator:doc.createElement('div'),backdrop:doc.createElement('div'),window:doc.createElement('div')});
    let sends=0;input.addEventListener('input',()=>sends++);
    layout.open();assert.equal(conversation.parentNode,chatSlot);assert.notEqual(conversation.inert,true);assert.equal(input.value,'保留未发送的要求');
    await input.dispatch('input');assert.equal(sends,1);layout.close();assert.equal(conversation.parentNode,home);assert.equal(input.value,'保留未发送的要求');
    layout.open();layout.destroy();assert.equal(conversation.parentNode,home);cases++;
  }
  {
    const c=clock();let status='进行中',unreachable=false,requests=0;
    const current=()=>({version:'workflow-'+status,task:{...view('').task,mode:'workflow',status,
      execution_status:status==='已阻塞'?'blocked':'running',last_activity_at:1000},
      plan:[{id:'workflow',status,steps:[{id:'work',status,detail:{last_activity_at:1000}}]}],
      structure:{nodes:[{id:'work',status}]}});
    const store=createStore({...c,api:{readView:async()=>{
      requests++;if(unreachable)throw new Error('host exited');return current();},listTasks:async()=>({tasks:[]})}});
    store.setScope(scope);await c.tick();await c.tick(300100);
    assert.ok(requests>10,'Keep observing the actual conversation lifecycle');
    assert.equal(store.state.view.task.status,'进行中','Silence alone must not fabricate a stopped conversation');
    status='已阻塞';store.refresh();await c.tick(250);
    assert.equal(store.state.view.task.status,'已阻塞');
    assert.equal(store.state.view.plan[0].steps[0].status,'已阻塞');
    assert.equal(store.state.view.structure.nodes[0].status,'已阻塞');
    assert.equal(store.state.view.task.last_activity_at,1000);
    unreachable=true;await c.tick(60000);
    assert.equal(store.state.view.task.status,'已阻塞','Loss of the host must not leave a running card');
    assert.equal(store.state.view.task.stalled_seconds,undefined);
    assert.ok(!VIEW_ERROR.includes('不受影响'));
    unreachable=false;status='进行中';store.refresh();await c.tick(250);
    assert.equal(store.state.view.task.status,'进行中');
    assert.equal(store.state.view.plan[0].steps[0].status,'进行中');
    store.dispose();cases++;
  }
  {
    const c=clock();let status='needs_input',requests=0;
    const current=()=>({...view(status),task:{...view('').task,mode:'workflow',execution_status:status,
      controls:status==='paused'?['resume','cancel']:status==='completed'?[]:['cancel']}});
    const store=createStore({...c,api:{readView:async()=>{requests++;return current();},listTasks:async()=>({tasks:[]})}});
    store.setScope(scope);await c.tick();
    status='paused';await c.tick(10000);
    assert.equal(store.state.view.task.execution_status,'paused','Saved changes must become visible while waiting for input');
    assert.ok(store.state.view.task.controls.includes('resume'));
    status='completed';await c.tick(10000);
    assert.equal(store.state.view.task.execution_status,'completed','Paused runs must observe external continuation');
    await c.tick(10000);const settled=requests;await c.tick(60000);
    assert.equal(requests,settled,'Completed runs do not poll forever');
    store.dispose();cases++;
  }
  console.log(`workbench_store: ${cases} cases passed (dedup, frame coalescing, rolling budget, selection/scope fences, reconnect, errors, API, workspace focus, conversation lifecycle)`);
})().catch(error => { console.error(error); process.exitCode = 1; });
