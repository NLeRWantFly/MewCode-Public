// Visual/layout verification for the real waiting-area CSS.
const {app, BrowserWindow} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');

const root = path.resolve(__dirname, '..');
const output = path.resolve(process.argv[2]);
app.setPath('userData', path.join(output, 'profile'));
app.disableHardwareAcceleration();

const fixture = `<!doctype html><html data-theme="light"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/_tokens.css"><link rel="stylesheet" href="/static/_primitives.css">
<link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/chat-queue.css">
<style>body{display:block;margin:0;padding:24px;background:var(--bg)}*{transition:none!important;animation:none!important}.input-wrap{max-width:none;margin:0 auto}</style>
</head><body><div class="input-wrap">
<div id="chat-queue" class="chat-queue" aria-live="polite" aria-label="等待发送的消息">
  <div class="chat-queue-header"><strong>等待发送 · 2</strong><span>当前回复结束后按顺序发送</span></div>
  <ol class="chat-queue-list">
    <li class="chat-queue-item"><span class="chat-queue-position">1</span><div class="chat-queue-copy"><span class="chat-queue-text">请基于刚才的结果再检查一次全部边界条件，尤其注意并发更新</span><small class="chat-queue-attachments">requirements.pdf、reference.png</small></div><div class="chat-queue-actions"><button class="chat-queue-send-now" type="button"><span>立即发送</span><kbd>↵</kbd></button><button class="chat-queue-remove" type="button">×</button></div></li>
    <li class="chat-queue-item"><span class="chat-queue-position">2</span><div class="chat-queue-copy"><span class="chat-queue-text">然后给出最终摘要</span></div><div class="chat-queue-actions"><button class="chat-queue-send-now" type="button"><span>立即发送</span><kbd>↵</kbd></button><button class="chat-queue-remove" type="button">×</button></div></li>
  </ol>
</div><textarea rows="1" aria-label="消息" placeholder="可以继续输入下一条消息"></textarea>
</div></body></html>`;

const server = http.createServer((request, response) => {
  if (request.url === '/') {
    response.setHeader('Content-Type', 'text/html; charset=utf-8');
    response.end(fixture);
    return;
  }
  const file = path.join(root, 'frontend', request.url);
  if (!file.startsWith(path.join(root, 'frontend', 'static') + path.sep)) {
    response.statusCode = 404;
    response.end();
    return;
  }
  response.setHeader('Content-Type', 'text/css');
  try { response.end(fs.readFileSync(file)); }
  catch { response.statusCode = 404; response.end(); }
});

app.whenReady().then(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const win = new BrowserWindow({
    show: false,
    width: 720,
    height: 430,
    webPreferences: {offscreen: true, backgroundThrottling: false, contextIsolation: true},
  });
  try {
    await win.loadURL(`http://127.0.0.1:${server.address().port}/`);
    const reports = [];
    for (const width of [320, 430, 720]) {
      win.setSize(width, 430);
      await new Promise(resolve => setTimeout(resolve, 80));
      const report = await win.webContents.executeJavaScript(`(() => { try {
        const queue=document.getElementById('chat-queue');
        const wrap=document.querySelector('.input-wrap');
        const outer=wrap.getBoundingClientRect();
        if(queue.scrollWidth>queue.clientWidth+1)throw new Error('queue horizontal overflow');
        const actions=[...queue.querySelectorAll('button')];
        for(const button of actions){
          // The queue intentionally scrolls within its height budget. Check
          // hit testing after bringing each action into that scroll viewport.
          button.scrollIntoView({block:'nearest'});
          const box=button.getBoundingClientRect();
          if(box.width<20||box.height<20||box.left<outer.left||box.right>outer.right+1)throw new Error('clipped queue action');
          if(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2)?.closest('button')!==button)throw new Error('obscured queue action '+JSON.stringify({width:innerWidth,height:innerHeight,box:box.toJSON(),text:button.textContent,hit:document.elementFromPoint(box.x+box.width/2,box.y+box.height/2)?.outerHTML}));
        }
        const copies=[...queue.querySelectorAll('.chat-queue-copy')];
        if(copies.some(node=>node.getBoundingClientRect().width<1))throw new Error('message summary collapsed');
        return {width:innerWidth,actions:actions.length,queueHeight:Math.round(queue.getBoundingClientRect().height),compact:getComputedStyle(document.querySelector('.chat-queue-send-now > span')).display==='none'};
      } catch(error) { return {error: String(error.stack || error)}; } })()`);
      if (report.error) throw new Error(report.error);
      reports.push(report);
    }
    win.setSize(720, 430);
    await new Promise(resolve => setTimeout(resolve, 120));
    win.webContents.invalidate();
    fs.writeFileSync(path.join(output, 'chat-queue.png'), (await win.webContents.capturePage()).toPNG());
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(reports));
    console.log(JSON.stringify(reports));
    win.destroy();
    server.close();
    app.exit(0);
  } catch (error) {
    fs.writeFileSync(path.join(output, 'error.txt'), String(error.stack || error));
    console.error(error.stack || error);
    win.destroy();
    server.close();
    app.exit(1);
  }
});
