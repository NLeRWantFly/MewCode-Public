// Exercise real composer HTML/CSS, without loading the application or a live backend.
const {app,BrowserWindow}=require('electron');
const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]);
app.setPath('userData',path.join(output,'profile'));app.disableHardwareAcceleration();
const html=fs.readFileSync(path.join(root,'frontend/static/index.html'),'utf8');
const composer=html.slice(html.indexOf('<div class="composer-shell">'),html.indexOf('<div id="status-line">'));
const fixture=`<!doctype html><html data-theme="light"><head><meta charset="UTF-8"><link rel="stylesheet" href="/static/_tokens.css"><link rel="stylesheet" href="/static/_primitives.css"><link rel="stylesheet" href="/static/style.css"><style>
body{display:block;padding:24px;background:var(--bg);height:auto} .hidden{display:none!important}
.composer-shell{margin:0 auto;max-width:none} *{transition:none!important;animation:none!important}
</style></head><body>${composer}</body></html>`;
const server=http.createServer((req,res)=>{
  if(req.url==='/'){res.setHeader('Content-Type','text/html; charset=utf-8');res.end(fixture);return;}
  const file=path.join(root,'frontend',req.url);
  if(!file.startsWith(path.join(root,'frontend','static')+path.sep)){res.statusCode=404;res.end();return;}
  res.setHeader('Content-Type',file.endsWith('.svg')?'image/svg+xml':'text/css');
  try{res.end(fs.readFileSync(file));}catch{res.statusCode=404;res.end();}
});
app.whenReady().then(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const win=new BrowserWindow({show:false,width:1500,height:700,webPreferences:{offscreen:true,backgroundThrottling:false,contextIsolation:true}});
  try{
    await win.loadURL(`http://127.0.0.1:${server.address().port}/`);
    const reports=[];
    for(const zoom of [1,1.25,1.5]){
      win.webContents.setZoomFactor(zoom);
      const report=await win.webContents.executeJavaScript(`(async()=>{
        const results=[];const shell=document.querySelector('.composer-shell');
        const ids=['attach-btn','perm-mode-btn','multi-agent-btn','model-effort-btn','composer-ctx-ring','btn-stop','btn-send'];
        document.getElementById('model-effort-label').textContent='gpt-6-astra · medium · 100 / very-long-provider-model-name';
        document.getElementById('perm-mode-label').textContent='计划';
        for(const running of [false,true])for(const width of [280,320,360,480,640,780,975]){
          shell.style.width=width+'px';
          document.getElementById('btn-stop').classList.toggle('hidden',!running);
          await new Promise(r=>requestAnimationFrame(r));
          const bounds=document.querySelector('.input-wrap').getBoundingClientRect();
          const buttons=ids.filter(id=>running||id!=='btn-stop').map(id=>document.getElementById(id));
          for(const button of buttons){
            const b=button.getBoundingClientRect();
            if(b.width<20||b.height<20||b.left<bounds.left||b.right>bounds.right+1||b.bottom>bounds.bottom+1)throw new Error(width+'px clipped '+button.id);
            if(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2)?.closest('button,a')!==button)throw new Error(width+'px obscured '+button.id+' by '+document.elementFromPoint(b.x+b.width/2,b.y+b.height/2)?.outerHTML.slice(0,350)+'; '+JSON.stringify(b.toJSON()));
          }
          for(let i=0;i<buttons.length;i++)for(let j=i+1;j<buttons.length;j++){
            const a=buttons[i].getBoundingClientRect(),b=buttons[j].getBoundingClientRect();
            if(a.left<b.right-1&&a.right>b.left+1&&a.top<b.bottom-1&&a.bottom>b.top+1)throw new Error('Overlapping '+buttons[i].id+'/'+buttons[j].id);
          }
          results.push({width,running,visible:buttons.length});
        }
        return results;
      })()`);
      reports.push({zoom,cases:report.length});
    }
    win.webContents.setZoomFactor(1.25);
    await win.webContents.executeJavaScript(`document.querySelector('.composer-shell').style.width='780px'`);
    await new Promise(r=>setTimeout(r,200));win.webContents.invalidate();
    fs.writeFileSync(path.join(output,'composer.png'),(await win.webContents.capturePage()).toPNG());
    fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(reports));
    console.log(JSON.stringify(reports));win.destroy();server.close();app.exit(0);
  }catch(error){fs.writeFileSync(path.join(output,'error.txt'),String(error.stack));fs.writeFileSync(path.join(output,'failure.png'),(await win.webContents.capturePage()).toPNG());console.error(error);server.close();app.exit(1);}
});
