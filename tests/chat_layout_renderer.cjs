// Actual ordinary chat and question CSS in a hidden, loopback-only renderer.
const {app,BrowserWindow}=require('electron');
const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]);
app.setPath('userData',path.join(output,'profile'));app.disableHardwareAcceleration();
const html=fs.readFileSync(path.join(root,'frontend/static/index.html'),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'');
let apiWrites=0;
const server=http.createServer((req,res)=>{
  const url=req.url.split('?')[0];
  if(url==='/'){res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html);return;}
  if(url.startsWith('/api/')){apiWrites++;res.statusCode=500;res.end();return;}
  const file=path.resolve(root,'frontend','.'+url);
  if(!file.startsWith(path.join(root,'frontend','static')+path.sep)){res.statusCode=404;res.end();return;}
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html; charset=utf-8');
  try{
    let data=fs.readFileSync(file);
    res.end(data);
  }catch{res.statusCode=404;res.end();}
});
app.whenReady().then(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const origin=`http://127.0.0.1:${server.address().port}`;
  const win=new BrowserWindow({show:false,width:1280,height:850,webPreferences:{offscreen:true,contextIsolation:true,backgroundThrottling:false}});
  win.webContents.session.webRequest.onBeforeRequest((details,cb)=>cb({cancel:!details.url.startsWith(origin+'/')}));
  const reports=[];
  try{
    await win.loadURL(origin+'/');
    await win.webContents.executeJavaScript(`(()=>{
      document.getElementById('drawer')?.remove();
      document.getElementById('drawer-backdrop')?.remove();
      document.querySelectorAll('dialog').forEach(n=>n.close());
      const messages=document.getElementById('messages');
      messages.innerHTML='<div class="msg user"><div class="msg-bubble"><div class="md">请按确认的阶段图继续，保留验收证据。</div></div></div><div class="msg assistant orch-stage-msg"><div class="msg-bubble"><div class="orch-stage-msg-head"><strong>实现与验证</strong><span>当前模型</span><button class="orch-chat-details">调用详情</button></div><div class="md"><p>已完成范围内修改，正在检查验收日志。</p><pre><code>tests: passed</code></pre><p>缺少的证据会单独标记，不会当作通过。</p></div><details class="tool-group"><summary>2 次工具调用 · 已完成</summary><p>测试日志与文件检查结果</p></details></div></div>';
      const style=document.createElement('style');style.textContent='*{animation:none!important;transition:none!important}';document.head.appendChild(style);
    })()`);
    for(const theme of ['light','dark'])for(const width of [390,980,1280]){
      win.setSize(width,850);
      const result=await win.webContents.executeJavaScript(`(async()=>{
        document.documentElement.dataset.theme=${JSON.stringify(theme)};
        await new Promise(r=>requestAnimationFrame(r));
        const bubble=document.querySelector('.msg.assistant .msg-bubble'),style=getComputedStyle(bubble);
        if(style.backgroundColor==='rgba(0, 0, 0, 0)'||parseFloat(style.borderTopWidth)<=0)throw new Error('Invisible message bubble');
        const msg=bubble.getBoundingClientRect(),input=document.querySelector('.input-wrap').getBoundingClientRect();
        if(Math.abs(msg.left-input.left)>1||Math.abs(msg.right-input.right)>1)throw new Error('Message/composer columns misaligned '+JSON.stringify({msg:msg.toJSON(),input:input.toJSON()}));
        if(msg.left<0||msg.right>innerWidth||input.bottom>innerHeight)throw new Error('Clipped chat');
        if(document.getElementById('messages').clientHeight<170)throw new Error('Composer starves chat viewport '+JSON.stringify({width:innerWidth,chat:document.getElementById('messages').clientHeight}));
        return {theme:${JSON.stringify(theme)},width:innerWidth,bubbles:true,aligned:true};
      })()`);
      reports.push(result);
      win.webContents.invalidate();
      fs.writeFileSync(path.join(output,`chat-${theme}-${width}.png`),(await win.webContents.capturePage()).toPNG());
    }
    const askUserChoices=await win.webContents.executeJavaScript(require('./ask_user_choices_renderer.cjs'));
    const questionLayouts=[];
    for(const theme of ['light','dark'])for(const width of [390,1280]){
      win.setSize(width,850);
      const layout=await win.webContents.executeJavaScript(`(async()=>{
        document.documentElement.dataset.theme=${JSON.stringify(theme)};
        document.getElementById('messages').replaceChildren();
        window.showAskUserFixture();
        await new Promise(r=>requestAnimationFrame(r));
        const panel=document.getElementById('chat-human-oncall'),bounds=panel.getBoundingClientRect();
        if(panel.scrollWidth>panel.clientWidth+1||bounds.left<0||bounds.right>innerWidth||bounds.bottom>innerHeight)throw new Error('Question panel clipped');
        if(panel.querySelector('.ask-user-details').open)throw new Error('Question history expanded');
        for(const card of panel.querySelectorAll('.human-oncall-option')){
          if(card.scrollWidth>card.clientWidth+1)throw new Error('Choice text clipped');
          if(getComputedStyle(card).display==='none')throw new Error('Choice hidden');
        }
        const submit=document.getElementById('human-oncall-submit').getBoundingClientRect();
        if(submit.bottom>innerHeight||submit.height<30)throw new Error('Submit unavailable');
        return {theme:${JSON.stringify(theme)},width:innerWidth,choices:2};
      })()`);
      questionLayouts.push(layout);
      await new Promise(r=>setTimeout(r,80));
      win.webContents.invalidate();fs.writeFileSync(path.join(output,'question-'+theme+'-'+width+'.png'),(await win.webContents.capturePage()).toPNG());
    }
    fs.writeFileSync(path.join(output,'chat-report.json'),JSON.stringify({cases:reports,askUserChoices,questionLayouts}));
    console.log('CHAT_RENDERER '+JSON.stringify(reports));win.destroy();server.close();app.exit(0);
  }catch(error){fs.writeFileSync(path.join(output,'chat-failure.png'),(await win.webContents.capturePage()).toPNG());console.error(error);server.close();app.exit(1);}
});
