// Production chat, layout and rendering in an isolated loopback-only browser.
const {app, BrowserWindow} = require('electron');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const root = path.resolve(__dirname, '..'), output = path.resolve(process.argv[2]);
app.setPath('userData', path.join(output, 'profile')); app.disableHardwareAcceleration();
let active = 'first';
const entries = Array.from({length: 100}, (_, i) => ({role: i % 2 ? 'assistant' : 'user',
  text: `第 ${i} 条消息。` + '这是用于验证长对话滚动和执行显示的内容。'.repeat(8)}));
const state = () => ({status: {conversation_id: active, scope: 'project', work_dir: '', expert_mode: false},
  slots: [], efforts: {levels: [], current: 'auto'}, running_conversations: []});
const server = http.createServer((req, res) => {
  const url = req.url.split('?')[0];
  if (url === '/__fixture__') {
    res.setHeader('Content-Type', 'application/json');
    res.end(process.argv[3] ? fs.readFileSync(process.argv[3]) : 'null');return;
  }
  if (url.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    if (url.startsWith('/api/conversations/project/')) {
      active = url.split('/').at(-1);
      if (active === 'reference' && process.argv[3]) {
        const saved=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
        const runs=saved.expert_runs || [saved];
        res.end(JSON.stringify({...saved,id:active,entries:saved.entries || [],
          expert_runs:runs.map(run=>({...run,conversation_id:active,
            events:run.events.map(event=>({...event,conversation_id:active}))}))}));
      } else res.end(JSON.stringify({id: active, entries, expert_runs: []}));
    } else if (url === '/api/state') res.end(JSON.stringify(state()));
    else if (url === '/api/workbench/view') res.end(JSON.stringify({version: 'empty', task: null, inbox: []}));
    else res.end(JSON.stringify({tasks: [], models: [], projects: [], conversations: []}));
    return;
  }
  const file = url === '/' ? path.join(root, 'frontend/static/index.html') : path.resolve(root, 'frontend', '.' + url);
  if (!file.startsWith(path.join(root, 'frontend/static') + path.sep)) {res.statusCode = 404; res.end(); return;}
  try {
    let data = fs.readFileSync(file, 'utf8');
    if (url === '/static/app.js') data = data.replace('void bootstrapWorkspace();',
      'window.__chatTest = {openConversation, setConversationView, mirrorExpertChatEvent, renderExpertSnapshot};');
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html; charset=utf-8');
    res.end(data);
  } catch {res.statusCode = 404; res.end();}
});
app.whenReady().then(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const win = new BrowserWindow({show: false, width: 1280, height: 850,
    webPreferences: {offscreen: true, contextIsolation: true, backgroundThrottling: false}});
  win.webContents.on('console-message', event => {
    if (event.message?.startsWith('REFERENCE_RENDER ')) process.stdout.write(event.message+'\n');
  });
  win.webContents.session.webRequest.onBeforeRequest((details, callback) => callback({cancel: !details.url.startsWith(origin + '/')}));
  try {
    await win.loadURL(origin + '/');
    const result = await win.webContents.executeJavaScript(`(async () => {
      const assert=(value,message)=>{if(!value)throw new Error(message);};
      const settle=()=>new Promise(resolve=>setTimeout(resolve,120));
      const messages=document.getElementById('messages'), test=window.__chatTest;
      const bottom=()=>messages.scrollHeight-messages.clientHeight-messages.scrollTop<3;
      const style=document.createElement('style');style.textContent='*{animation:none!important;transition:none!important}';document.head.append(style);
      assert(test && window.MewWorkbench,'Production app failed to initialize');
      await test.openConversation('project','first');await settle();
      assert(bottom(),'Opening a conversation did not show the latest message');
      document.getElementById('workbench-toggle').click();await settle();
      assert(bottom(),'Opening the graph lost the chat tail: '+messages.scrollTop);
      document.querySelector('[aria-label="收起工作台"]').click();await settle();
      assert(bottom(),'Closing the graph lost the chat tail');
      messages.scrollTop=messages.scrollHeight*.4;await settle();
      const anchor=[...messages.children].find(el=>el.getBoundingClientRect().bottom>messages.getBoundingClientRect().top);
      const offset=()=>anchor.getBoundingClientRect().top-messages.getBoundingClientRect().top;
      const before=offset();
      document.getElementById('workbench-toggle').click();await settle();
      assert(Math.abs(offset()-before)<3,'Graph opening moved the message being read');
      document.querySelector('[aria-label="收起工作台"]').click();await settle();
      assert(Math.abs(offset()-before)<3,'Graph closing moved the message being read');
      const savedTop=messages.scrollTop;
      await test.openConversation('project','second');await settle();assert(bottom(),'New conversation inherited old scroll position');
      await test.openConversation('project','first');await settle();
      assert(Math.abs(messages.scrollTop-savedTop)<3,'Returning to a conversation lost its reading position');
      test.setConversationView('trajectory');await test.openConversation('project','third');await settle();
      test.setConversationView('chat');await settle();assert(bottom(),'History loaded while hidden stayed at the top');
      const base={conversation_id:'third',run_id:'live',node_id:'execute',role:'executor',attempt:1,node_attempt:1};
      test.mirrorExpertChatEvent({...base,type:'expert_node_started',status:'running'});
      const originalRender=window.MewMarkdown.renderMarkdown;let renders=0;
      window.MewMarkdown.renderMarkdown=(...args)=>{renders++;return originalRender(...args);};
      for(let i=0;i<200;i++)test.mirrorExpertChatEvent({...base,type:'expert_delta',text:'执行内容。'});
      await settle();
      window.MewMarkdown.renderMarkdown=originalRender;
      assert(renders<10,'Every stream fragment reparsed the whole Markdown response: '+renders);
      const bubble=messages.querySelector('.orch-stage-msg');
      assert(bubble.querySelector('.md').textContent.trim()==='执行内容。'.repeat(200),'Live text missing or duplicated');
      assert(bottom(),'Live output no longer follows the tail');
      document.getElementById('workbench-toggle').click();await settle();
      document.querySelector('[aria-label="收起构图对话"]').click();
      test.mirrorExpertChatEvent({...base,type:'expert_delta',text:'隐藏时也有输出。'});await settle();
      document.querySelector('.wb-rail [aria-label="对话"]').click();await settle();
      assert(bottom(),'Reopening the chat panel lost output received while hidden');
      document.querySelector('[aria-label="收起工作台"]').click();await settle();
      messages.scrollTop=messages.scrollHeight*.25;await settle();const reading=messages.scrollTop;
      test.mirrorExpertChatEvent({...base,node_id:'another',type:'expert_node_started',status:'running'});
      test.mirrorExpertChatEvent({...base,type:'expert_delta',text:'继续执行。'});await settle();
      assert(Math.abs(messages.scrollTop-reading)<3,'A new execution stole the reading position');
      const reference=await fetch('/__fixture__').then(r=>r.json());
      if(reference) {
        const started=performance.now();
        await test.openConversation('project','reference');
        await settle();
        assert(messages.querySelectorAll('.tg-row').length>0,'Saved running task did not restore tool receipts');
        assert(bottom(),'The full saved conversation did not open at the tail');
        console.log('REFERENCE_RENDER '+JSON.stringify({milliseconds:Math.round(performance.now()-started),
          messages:messages.querySelectorAll('.orch-stage-msg').length,tools:messages.querySelectorAll('.tg-row').length}));
      }
      return {conversationTail:true,graphTail:true,readingAnchor:true,conversationRestore:true,hiddenHistory:true,liveText:true,readingPreserved:true};
    })()`);
    fs.mkdirSync(output, {recursive:true});fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(result,null,2));
    process.stdout.write('SCROLL_RESULT '+JSON.stringify(result)+'\n');win.destroy();app.exit(0);
  } catch (error) {process.stderr.write(String(error.stack || error)+'\n');win.destroy();app.exit(1);}
  finally {server.close();}
});
