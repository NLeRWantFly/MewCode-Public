/* Exercise the production composer handlers without a live desktop/model. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../frontend/static/app.js'), 'utf8');
const start = source.indexOf('  async function sendChat(');
const end = source.indexOf('  window.addEventListener("mewcode:chat-queue-error"', start);
assert.ok(start >= 0 && end > start);

function fixture() {
  const calls = [], messages = [], notices = [];
  const button = {disabled:false, addEventListener(_name, fn) { this.click = fn; }};
  const sendBtn = {addEventListener(_name, fn) { this.click = fn; }};
  const context = {
    window: {}, crypto: require('node:crypto').webcrypto,
    conversationNavigationPending:false, streaming:false, readonlyView:false,
    inputEl:{value:'仅继续暂存两份文件；再次拒绝就停止。', style:{}, dispatchEvent() {}},
    pendingAttachments:[], attachmentUploadBusy:false, ws:{readyState:1}, WebSocket:{OPEN:1},
    modelCatalog:null, currentStatus:{}, activeId:'conversation-1',
    activeConversationId() { return context.activeId; },
    $(id) { assert.equal(id, 'continue-task-btn'); return button; },
    sendBtn, conversationEventBuffers:new Map(), currentExpertTimeline:null,
    parseSlashCommand:() => null,
    addUserMsg:() => ({isConnected:true, remove() {}}),
    hideSlashMenu() {}, renderAttachmentTray() {}, markConversationStartPending() {},
    notify(text) { notices.push(text); }, addErrorMsg(text) { notices.push(text); },
    addSystemMsg(text) { notices.push(text); }, refreshState() {},
    send(frame) { messages.push(frame); return true; },
    async fetch(url, options) {
      calls.push({url, options});
      return {ok:true, json:async()=>({conversation_id:'conversation-1', task_id:'task-1', task_kind:'chat'})};
    },
  };
  vm.runInNewContext(source.slice(start, end), context);
  return {context, button, sendBtn, calls, messages, notices};
}

test('explicit continuation sends current identity and long text, without any grant POST', async()=>{
  const f=fixture(); const draft=f.context.inputEl.value;
  await f.button.click();
  assert.equal(f.calls.length,1); assert.equal(f.calls[0].options,undefined);
  assert.equal(f.calls[0].url,'/api/chat/task-authorization');
  assert.equal(f.messages.length,1);
  assert.equal(f.messages[0].text,draft);
  assert.equal(f.messages[0].continuation_task_id,'task-1');
  assert.equal(f.messages[0].conversation_id,'conversation-1');
  assert.equal(f.context.inputEl.value,'');
  assert.equal(f.button.disabled,false);
});

test('ordinary send never inherits the continuation binding', async()=>{
  const f=fixture(); await f.button.click();
  f.context.inputEl.value='另一个任务'; await f.sendBtn.click();
  assert.equal(f.messages.length,2);
  assert.equal('continuation_task_id' in f.messages[1],false);
  assert.equal(f.calls.length,1);
});

for (const changed of ['conversation','draft','attachments','busy','navigation']) {
  test(`do not send when ${changed} changes during task lookup`, async()=>{
    const f=fixture();
    f.context.fetch=async()=>{
      if(changed==='conversation') f.context.activeId='conversation-2';
      if(changed==='draft') f.context.inputEl.value='新草稿';
      if(changed==='attachments') f.context.pendingAttachments.push({id:'new'});
      if(changed==='busy') f.context.streaming=true;
      if(changed==='navigation') f.context.conversationNavigationPending=true;
      return {ok:true,json:async()=>({conversation_id:'conversation-1',task_id:'task-1',task_kind:'chat'})};
    };
    await f.button.click();
    assert.equal(f.messages.length,0); assert.ok(f.context.inputEl.value);
    assert.equal(f.notices.length,1); assert.equal(f.button.disabled,false);
  });
}

for (const result of [
  {ok:false,body:{error:'service unavailable'}},
  {ok:true,body:{conversation_id:'conversation-1',task_id:'flow',task_kind:'orchestration'}},
  {ok:true,body:{conversation_id:'conversation-1',task_kind:'chat'}},
]) {
  test(`reject unavailable or non-chat task ${JSON.stringify(result.body)}`, async()=>{
    const f=fixture(); f.context.fetch=async()=>({ok:result.ok,json:async()=>result.body});
    await f.button.click(); assert.equal(f.messages.length,0);
    assert.ok(f.context.inputEl.value); assert.equal(f.button.disabled,false);
  });
}

test('double click cannot send twice while lookup is pending', async()=>{
  const f=fixture(); let release;
  f.context.fetch=()=>new Promise(resolve=>{ release=resolve; });
  const pending=f.button.click(); await f.button.click();
  release({ok:true,json:async()=>({conversation_id:'conversation-1',task_id:'task-1',task_kind:'chat'})});
  await pending; assert.equal(f.messages.length,1);
});

test('empty draft does not silently submit or grant anything', async()=>{
  const f=fixture(); f.context.inputEl.value=''; await f.button.click();
  assert.equal(f.messages.length,0); assert.equal(f.calls.length,0);
});

test('slash command can switch maintenance modes through the existing command transport', async()=>{
  const f=fixture(); let sent=0;
  f.context.window.OrchestrationMaintenance={
    composerState:()=>({active:true,canSend:true}),send:()=>sent++,
  };
  f.context.inputEl.value='/update work 检查汇总';
  f.context.parseSlashCommand=()=>({name:'update',args:'work 检查汇总'});
  await f.sendBtn.click();
  assert.equal(sent,0);
  assert.equal(f.messages.length,1);
  assert.equal(f.messages[0].type,'command');
  assert.equal(f.messages[0].name,'update');
  assert.equal(f.calls.length,0);
});
