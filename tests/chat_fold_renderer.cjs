// 对话折叠：真实渲染验收。隐藏窗口 + loopback 静态服务，保留 app.js 真实执行，
// 注入消息后验证折叠/展开与摘要行为。
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const root = path.resolve(__dirname, '..'), output = path.resolve(process.argv[2]);
app.setPath('userData', path.join(output, 'profile'));
app.disableHardwareAcceleration();

// 保留全部脚本：app.js 依赖 vendor/markdown-render/plugin-runtime 等真实引导。
const html = fs.readFileSync(path.join(root, 'frontend/static/index.html'), 'utf8');

const server = http.createServer((req, res) => {
  const url = req.url.split('?')[0];
  if (url === '/') { res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(html); return; }
  const file = path.resolve(root, 'frontend', '.' + url);
  if (!file.startsWith(path.join(root, 'frontend', 'static') + path.sep)) { res.statusCode = 404; res.end(); return; }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html; charset=utf-8');
  try { res.end(fs.readFileSync(file)); } catch { res.statusCode = 404; res.end(); }
});

app.whenReady().then(async () => {
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const win = new BrowserWindow({
    show: false, width: 1280, height: 850,
    webPreferences: { offscreen: true, contextIsolation: true, backgroundThrottling: false },
  });
  win.webContents.session.webRequest.onBeforeRequest(
    (details, cb) => cb({ cancel: !details.url.startsWith(origin + '/') })
  );
  win.webContents.on('console-message', (_e, level, message, line, source) => {
    if (level >= 2) console.error('[console]', source + ':' + line, message);
  });
  try {
    await win.loadURL(origin + '/');
    await new Promise((r) => setTimeout(r, 400)); // 等 app.js 引导完成
    const result = await win.webContents.executeJavaScript(`(async()=>{
      const raf=()=>new Promise(r=>requestAnimationFrame(r));
      const style=document.createElement('style');
      style.textContent='*{animation:none!important;transition:none!important}';
      document.head.appendChild(style);
      const messages=document.getElementById('messages');
      if(!messages)throw new Error('messages missing');
      // 页面引导期自身可能写入的错误消息也算数：记录基线数量
      const before=messages.querySelectorAll(':scope > .msg.msg-fold-ready').length;
      const mk=(cls,txt)=>{const d=document.createElement('div');d.className='msg '+cls;
        d.innerHTML='<div class="msg-bubble"><div class="md">'+txt+'</div></div>';
        messages.appendChild(d);return d;};
      const user=mk('user','把这段很长的讨论折叠起来试试效果。');
      const ai=mk('assistant','这是一条很长的助手回复，用来验证折叠摘要。'.repeat(12));
      await raf();await raf();
      const btnUser=user.querySelector(':scope > .msg-fold-btn');
      const btnAi=ai.querySelector(':scope > .msg-fold-btn');
      if(!btnUser||!btnAi)throw new Error('fold button missing');
      btnAi.click();await raf();
      if(!ai.classList.contains('collapsed'))throw new Error('assistant not collapsed');
      if(getComputedStyle(ai.querySelector('.msg-bubble')).display!=='none')throw new Error('bubble still visible');
      const summary=ai.querySelector(':scope > .msg-fold-summary');
      if(summary.hidden||getComputedStyle(summary).display==='none')throw new Error('summary hidden');
      const excerpt=summary.querySelector('.msg-fold-excerpt').textContent;
      if(!excerpt.includes('折叠摘要'))throw new Error('excerpt not captured: '+excerpt);
      if(summary.querySelector('.msg-fold-tag').textContent!=='AI')throw new Error('role tag wrong');
      summary.click();await raf();
      if(ai.classList.contains('collapsed'))throw new Error('expand failed');
      if(getComputedStyle(ai.querySelector('.msg-bubble')).display==='none')throw new Error('bubble not restored');
      btnUser.click();await raf();
      if(!user.classList.contains('collapsed'))throw new Error('user not collapsed');
      if(user.querySelector(':scope > .msg-fold-summary').querySelector('.msg-fold-tag').textContent!=='你')
        throw new Error('user role tag wrong');
      const btnRect=btnUser.getBoundingClientRect(),msgRect=user.getBoundingClientRect();
      if(btnRect.right>msgRect.right+12)throw new Error('user fold button misaligned');
      if(btnRect.right>innerWidth||btnRect.left<0)throw new Error('fold button clipped');
      // 观察器对引导期消息同样生效（错误消息等）
      return {ok:true,bootDecorated:before,live:true};
    })()`);
    fs.writeFileSync(path.join(output, 'chat-fold-report.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
    app.exit(0);
  } catch (e) {
    console.error('[Error]', e.message);
    app.exit(1);
  } finally {
    server.close();
  }
});
