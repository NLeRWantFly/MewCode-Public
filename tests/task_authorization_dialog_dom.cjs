// Actual Chromium layout and interaction; no live app or permission endpoint.
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
app.setPath('userData', path.resolve(process.argv[2]));
app.disableHardwareAcceleration();
const root = path.resolve(__dirname, '..');
const output = path.resolve(process.argv[3] || process.argv[2]);
fs.mkdirSync(output, { recursive: true });
const source = fs.readFileSync(path.join(root, 'frontend/static/app.js'), 'utf8');
const start = source.indexOf('  $("task-auth-btn").addEventListener("click"');
const end = source.indexOf('  function answerPermission(', start);
if (start < 0 || end < 0) throw new Error('Task authorization source missing');
const css = ['_tokens.css', '_primitives.css', 'style.css'].map(name =>
  fs.readFileSync(path.join(root, 'frontend/static', name), 'utf8')).join('\n');
const html = `<!doctype html><html lang="zh-CN" data-theme="light"><meta charset="utf-8">
<style>${css}
/* Stable screenshots in a hidden renderer; motion is not under test. */
*, *::before, *::after { transition: none !important; animation: none !important; }
</style><body><button id="task-auth-btn">任务授权</button>
<div id="settings-overlay" class="settings-overlay hidden"><section id="settings-dialog" class="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-dialog-title">
<header class="settings-dialog-head"><div><span class="settings-eyebrow">MewCode Settings</span><h2 id="settings-dialog-title"></h2></div><button class="icon-btn" aria-label="关闭">×</button></header>
<div id="settings-dialog-body" class="settings-dialog-body"></div><footer id="settings-dialog-footer" class="settings-dialog-footer"></footer>
</section></div></body></html>`;
const harness = `
window.posts = []; window.metricsCalls = 0; window.notices = [];
window.current = {conversation_id:'conversation-1',task_id:'task-1',task_title:'完成 AUTO 验收：测试并提交两份文件 <img src=x>',authorization:null};
window.postMode = 'success';
const $ = id => document.getElementById(id);
const notify = (...args) => notices.push(args);
const loadApprovalMetrics = target => { metricsCalls++; target.textContent = '审批 12 次 · 本地放行 11 次 · 人工确认 1 次'; };
const closeSettingsDialog = () => $('settings-overlay').classList.add('hidden');
const openSettingsDialog = (title, trigger, build) => {
  $('settings-dialog-title').textContent=title;
  $('settings-dialog-body').replaceChildren(); $('settings-dialog-footer').replaceChildren();
  build($('settings-dialog-body'),$('settings-dialog-footer')); $('settings-overlay').classList.remove('hidden');
};
const fetch = async (url, options) => {
  if (!options?.method) return {ok:true,json:async()=>current};
  posts.push({url,options,payload:JSON.parse(options.body)});
  if(postMode==='pending') await new Promise(resolve=>window.finishPost=resolve);
  return postMode==='error' ? {ok:false,json:async()=>({error:'当前任务已变化，请刷新后重新授权'})} : {ok:true,json:async()=>({authorization:{version:'v1'}})};
};
${source.slice(start, end)}
window.tick = () => new Promise(resolve => setTimeout(resolve,20));
window.check = (condition, message) => { if(!condition) throw new Error(message); };
`;
const checks = String.raw`(async()=>{
 $('task-auth-btn').click(); await tick();
 check(posts.length===0,'opening must not grant permission');
 check(!document.querySelector('img'),'task title must be text, not HTML');
 check(!document.getElementById('task-auth-git-write').checked,'Git must not be preselected');
 check(!document.getElementById('task-auth-isolated-command').checked,'container must not be preselected');
 check(document.querySelectorAll('textarea').length===4,'all authorization fields retained');
 for(const control of document.querySelectorAll('textarea,select')) check(document.querySelector('label[for="'+control.id+'"]'),'accessible label missing');
 check(metricsCalls===0,'metrics must not delay primary form');
 document.querySelector('.task-auth-metrics').open=true; await tick();
 check(metricsCalls===1,'metrics should load on demand');
 document.querySelector('.task-auth-metrics').open=false;
 document.getElementById('task-auth-read_directories').value=[' C:/fixture/repository ', ' C:/fixture/input '].join('\n');
 document.getElementById('task-auth-write_directories').value='C:\\fixture\\repository';
 document.getElementById('task-auth-allowed_tools').value='ReadFile';
 document.getElementById('task-auth-git-write').checked=true;
 document.getElementById('task-auth-expiry').value='14400';
 postMode='error'; document.querySelector('.task-auth-confirm').click(); await tick();
 check(!document.querySelector('.task-auth-error').hidden,'save error must be visible');
 check(document.getElementById('task-auth-git-write').checked,'error lost selection');
 check(!document.querySelector('.task-auth-confirm').disabled,'save did not recover');
 const payload=posts[0].payload;
 check(posts[0].options.headers['X-Mew-Task-Authorization']==='1','explicit confirmation header lost');
 check(payload.conversation_id==='conversation-1' && payload.task_id==='task-1','task binding lost');
 check(payload.ttl_seconds===14400,'expiry changed');
 check(JSON.stringify(payload.allowed_behaviors)==='["git_write"]','behavior scope widened');
 check(payload.network_hosts.length===0 && payload.allowed_tools[0]==='ReadFile','fields changed');
 check(payload.read_directories.length===2,'multiline fields not normalized');
 postMode='pending'; const before=posts.length;
 document.querySelector('.task-auth-confirm').click(); document.querySelector('.task-auth-confirm').click(); await tick();
 check(posts.length===before+1,'duplicate submission');
 postMode='success'; finishPost(); await tick();
 check($('settings-overlay').classList.contains('hidden'),'success did not close');
 $('task-auth-btn').click(); await tick(); const cancelBefore=posts.length;
 document.querySelector('.task-auth-cancel').click(); await tick();
 check(posts.length===cancelBefore && $('settings-overlay').classList.contains('hidden'),'cancel submitted');
 current.authorization={allowed_behaviors:['git_write','isolated_command'],allowed_tools:['ReadFile'],network_hosts:['docs.python.org'],read_directories:['C:/中文 workspace 空格/repository'],write_directories:['C:/中文 workspace 空格/repository']};
 $('task-auth-btn').click(); await tick();
 check(document.getElementById('task-auth-git-write').checked && document.getElementById('task-auth-isolated-command').checked,'saved scope not preserved');
 check(document.querySelector('.task-auth-details').open,'existing advanced grant hidden');
 current.authorization=null; current.task_title='完成 AUTO 验收：测试并提交两份文件'; $('task-auth-btn').click(); await tick();
 return {scope:true,noImplicitGrants:true,errorRecovery:true,cancel:true,duplicateGuard:true};
})()`;
app.whenReady().then(async()=>{
 const win=new BrowserWindow({show:false,width:1000,height:950,webPreferences:{nodeIntegration:false,contextIsolation:true,backgroundThrottling:false}});
 win.webContents.session.webRequest.onBeforeRequest((d,cb)=>cb({cancel:!d.url.startsWith('data:') && d.url!=='about:blank'}));
 try {
  await win.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent(html));
  await win.webContents.executeJavaScript(harness + ';void 0;');
  const result=await win.webContents.executeJavaScript(checks);
  result.layouts=[];
  for(const [name,width,height,theme] of [['light',1000,950,'light'],['dark',1000,950,'dark'],['mobile',390,844,'light'],['short',800,500,'light']]) {
   win.setContentSize(width,height);
   await win.webContents.executeJavaScript('document.documentElement.dataset.theme='+JSON.stringify(theme));
   await new Promise(resolve=>setTimeout(resolve,180));
   const layout=await win.webContents.executeJavaScript(`(()=>{
    const dialog=document.getElementById('settings-dialog').getBoundingClientRect();
    const body=document.getElementById('settings-dialog-body');
    const button=document.querySelector('.task-auth-confirm').getBoundingClientRect();
    check(dialog.x>=0 && dialog.right<=innerWidth+1,'dialog horizontal overflow');
    check(body.scrollWidth<=body.clientWidth+1,'form horizontal overflow');
    check(button.bottom<=innerHeight+1 && button.top>=0,'confirm outside viewport');
    return {width:innerWidth,height:innerHeight,scrollable:body.scrollHeight>body.clientHeight};
   })()`);
   result.layouts.push({name,...layout});
   fs.writeFileSync(path.join(output, 'task-auth-'+name+'.png'), (await win.webContents.capturePage()).toPNG());
  }
  process.stdout.write('TASK_AUTH_DOM '+JSON.stringify(result)+'\n'); win.destroy(); app.exit(0);
 } catch(error) {process.stderr.write(String(error.stack || error)+'\n');win.destroy();app.exit(1);}
});
