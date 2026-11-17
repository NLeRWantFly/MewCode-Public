'use strict';
// Full-page Chromium harness; browser scenario stays independent of Electron.
const {app, BrowserWindow} = require('electron');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const output = path.resolve(process.env.MEW_BROWSER_OUTPUT || path.join(root, '.tmp/workbench-20261013/t95-browser'));
fs.mkdirSync(output, {recursive:true});
app.setPath('userData', path.join(output, 'profile'));
app.disableHardwareAcceleration();
const children = [], windows = [], errors = [], requests=[];
function closeFixtures(code) {
  for(const child of children)child.kill();
  for(const win of windows)if(!win.isDestroyed())win.destroy();
  app.exit(code);
}
const deadline=setTimeout(()=>{console.error('Browser scenario deadline exceeded');closeFixtures(1);},240000);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function server(mode) {
  const child = spawn(path.join(root, '.venv/Scripts/python.exe'), ['-m','tests.workbench_scenario_server','--mode',mode],
    {cwd:root, windowsHide:true, stdio:['ignore','pipe','pipe'], env:{...process.env,MEWCODE_OFFLINE_TEST:'1',PYTHONUTF8:'1'}});
  children.push(child); let log='';
  child.stderr.on('data', b => {log+=b; fs.writeFileSync(path.join(output,mode+'-server.log'),log);});
  return new Promise((resolve,reject) => {
    const timer=setTimeout(()=>reject(new Error('Fixture startup timeout: '+log)),30000);
    child.stdout.on('data',b=>{log+=b;fs.writeFileSync(path.join(output,mode+'-server.log'),log);
      const match=log.match(/WORKBENCH_SCENARIO (\{[^\n]+\})/);if(match){clearTimeout(timer);resolve('http://127.0.0.1:'+JSON.parse(match[1]).port);}});
    child.once('exit',code=>{clearTimeout(timer);if(code)reject(new Error(log));});
  });
}
async function tab() {
  const win=new BrowserWindow({show:false,width:1440,height:1000,webPreferences:{contextIsolation:true,nodeIntegration:false,backgroundThrottling:false,offscreen:true}});
  windows.push(win);
  win.webContents.session.webRequest.onCompleted((details)=>{requests.push({url:details.url,method:details.method,status:details.statusCode});fs.writeFileSync(path.join(output,'requests.json'),JSON.stringify(requests,null,2));});
  win.webContents.on('console-message',event=>{fs.appendFileSync(path.join(output,'console.log'),String(event.message)+'\n');if(event.level==='error'||event.level===3)errors.push(event.message);});
  win.webContents.on('render-process-gone',(_,details)=>errors.push(JSON.stringify(details)));
  return {evaluate:async source=>{try{return await win.webContents.executeJavaScript(source,true);}catch(error){throw new Error(source+'\n'+error.message+'\n'+errors.join('\n'));}},
    capture:async name=>{await win.webContents.executeJavaScript('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');await delay(200);fs.writeFileSync(path.join(output,name),(await win.webContents.capturePage(undefined,{stayHidden:true,stayAwake:true})).toPNG());},
    rpc:async(method,params={})=>{
      if(method==='Page.navigate')return win.loadURL(params.url);
      if(method==='Page.reload'){win.reload();await delay(200);return;}
      if(method==='Emulation.setDeviceMetricsOverride'){win.setSize(params.width,params.height);await delay(200);return;}
      throw new Error('Unsupported harness method: '+method);
    }, close:()=>win.close(), delay};
}
app.whenReady().then(async()=>{
  try{
    const base=await server('collaborative'),classicBase=await server('classic');
    const primary=await tab();
    const result=await require('../tests/workbench_browser_scenario.cjs')({...primary,base,classicBase,newTab:tab});
    const queueBase=await server('queue'),queueTab=await tab();
    result.chatQueue={...await require('../tests/chat_queue_browser_scenario.cjs')({...queueTab,base:queueBase}),engine:'Electron Chromium / production chat UI'};
    const autonomousBase=await server('autonomous'),autonomousTab=await tab();
    result.autonomous=await require('../tests/autonomous_browser_scenario.cjs')({...autonomousTab,base:autonomousBase});
    if(errors.length)throw new Error('Browser console errors: '+errors.join('\n'));
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({...result,engine:process.versions,consoleErrors:errors},null,2));
    console.log(JSON.stringify(result));
  }catch(error){
    console.error(error.stack);
    for(const [i,win] of windows.entries())if(!win.isDestroyed()){
      fs.writeFileSync(path.join(output,'failure-'+i+'.png'),(await win.webContents.capturePage()).toPNG());
      fs.writeFileSync(path.join(output,'failure-'+i+'.json'),JSON.stringify(await win.webContents.executeJavaScript(`({url:location.href,scope:window.MewWorkbench?.store.state.scope,state:window.MewWorkbench?.store.state,design:[...document.querySelectorAll('.wb-design-start button')].map(b=>({text:b.textContent,disabled:b.disabled})),errors:[...document.querySelectorAll('[role=status]')].map(n=>n.textContent)})`),null,2));
    }
    fs.writeFileSync(path.join(output,'failure.txt'),error.stack+'\n'+errors.join('\n'));process.exitCode=1;
  }
  finally{clearTimeout(deadline);closeFixtures(process.exitCode||0);}
});
