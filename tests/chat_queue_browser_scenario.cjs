'use strict';

// Full production index/app.js/chat-queue.js against a synthetic local stream.
module.exports = async ({base, evaluate, capture, rpc, delay}) => {
  const wait = expression => evaluate(`new Promise((resolve,reject)=>{let n=0;const timer=setInterval(async()=>{try{if(await (${expression})){clearInterval(timer);resolve(true);return;}}catch{}if(++n>400){clearInterval(timer);reject(new Error('Chat queue timeout: '+document.body.innerText.slice(-2500)));}},30);})`);
  const setDraftAndSend = text => evaluate(`(()=>{const input=document.getElementById('chat-input');input.value=${JSON.stringify(text)};input.dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('btn-send').click();})()`);
  const status = () => evaluate(`fetch('/__test__/status').then(response=>response.json())`);

  await rpc('Page.navigate', {url: base + '/desktop-boot?session=chat-queue-fixture-only&next=/'});
  await wait(`fetch('/__test__/status').then(r=>r.json()).then(s=>s.opened && window.MewChatQueue && window.MewConversationContext?.getContext()?.available && !document.getElementById('btn-send').disabled)`);
  await delay(100);

  await setDraftAndSend('当前长回复');
  await wait(`document.getElementById('btn-send').title==='加入等待区' && document.getElementById('messages').textContent.includes('正在处理：当前长回复')`);
  await setDraftAndSend('顺序消息一');
  await wait(`window.MewChatQueue.getState().items.length===1 && document.getElementById('chat-input').value===''`);
  await setDraftAndSend('顺序消息二');
  await wait(`window.MewChatQueue.getState().items.length===2 && document.getElementById('chat-input').value===''`);
  await evaluate(`(()=>{const users=document.querySelectorAll('#messages .msg.user');if(users.length!==1)throw new Error('Queued messages entered transcript before dispatch');const rows=[...document.querySelectorAll('.chat-queue-item')].map(row=>row.querySelector('.chat-queue-text').textContent);if(JSON.stringify(rows)!==JSON.stringify(['顺序消息一','顺序消息二']))throw new Error('Waiting area order changed');})()`);
  await capture('chat-queue-waiting.png');
  await evaluate(`fetch('/__test__/release',{method:'POST'})`);
  await wait(`fetch('/__test__/status').then(r=>r.json()).then(s=>!s.running&&s.submissions.length===3)`);
  let first = await status();
  if (JSON.stringify(first.submissions) !== JSON.stringify(['当前长回复', '顺序消息一', '顺序消息二'])) {
    throw new Error('Automatic dispatch order changed: ' + JSON.stringify(first.submissions));
  }
  await wait(`window.MewChatQueue.getState().items.length===0 && document.querySelectorAll('#messages .msg.user').length===3 && !document.getElementById('btn-send').disabled`);

  await setDraftAndSend('第二个长回复');
  await wait(`document.getElementById('btn-send').title==='加入等待区' && document.getElementById('messages').textContent.includes('正在处理：第二个长回复')`);
  await setDraftAndSend('保留消息 A');
  await wait(`window.MewChatQueue.getState().items.length===1`);
  await setDraftAndSend('优先消息 B');
  await wait(`window.MewChatQueue.getState().items.length===2`);
  await capture('chat-queue-before-send-now.png');
  await evaluate(`(()=>{const row=[...document.querySelectorAll('.chat-queue-item')].find(node=>node.querySelector('.chat-queue-text')?.textContent==='优先消息 B');if(!row)throw new Error('Priority queue row missing');row.querySelector('[data-chat-queue-send-now]').click();})()`);
  await wait(`document.getElementById('messages').textContent.includes('已自动处理等待消息：优先消息 B') && window.MewChatQueue.getState().items.length===1`);
  await evaluate(`(()=>{const items=window.MewChatQueue.getState().items;if(items.length!==1||items[0].text!=='保留消息 A')throw new Error('Send-now discarded the remaining queue');})()`);
  await capture('chat-queue-send-now-priority.png');
  await wait(`fetch('/__test__/status').then(r=>r.json()).then(s=>!s.running&&s.submissions.length===6)`);
  const second = await status();
  if (JSON.stringify(second.submissions.slice(-3)) !== JSON.stringify(['第二个长回复', '优先消息 B', '保留消息 A'])) {
    throw new Error('Send-now did not prioritize exactly one item: ' + JSON.stringify(second.submissions));
  }
  if (second.interrupts !== 1) throw new Error('Send-now did not interrupt exactly once');
  await wait(`window.MewChatQueue.getState().items.length===0 && document.querySelectorAll('#messages .msg.user').length===6 && !document.getElementById('btn-send').disabled`);

  await rpc('Page.reload');
  await wait(`window.MewChatQueue && window.MewConversationContext?.getContext()?.available && document.querySelectorAll('#messages .msg.user').length===6`);
  if (await evaluate(`document.documentElement.scrollWidth>innerWidth+1`)) throw new Error('Queue flow introduced horizontal overflow');
  await capture('chat-queue-final.png');
  return {
    engine: 'Headless Edge / full production chat JS',
    syntheticProvider: true,
    sequentialDispatch: true,
    sendNowPrioritized: true,
    remainingQueuePreserved: true,
    transcriptDeferredUntilDispatch: true,
    reloadRestoredHistory: true,
  };
};
