const {app,BrowserWindow}=require('electron');
const fs=require('node:fs'),path=require('node:path');
const url=process.argv[2], output=path.resolve(process.argv[3]);
app.setPath('userData',path.join(output,'renderer-profile'));app.disableHardwareAcceleration();
app.whenReady().then(async()=>{
  const win=new BrowserWindow({show:false,width:1600,height:1000,webPreferences:{contextIsolation:true,nodeIntegration:false}});
  const errors=[];win.webContents.on('console-message',(_event,level,message)=>{if(level>=3)errors.push(message);});
  try{
    await win.loadURL(url+'/workflow-acceptance');
    const result=await win.webContents.executeJavaScript(fs.readFileSync(path.join(__dirname,'scenario.js'),'utf8'));
    await win.webContents.executeJavaScript("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))");
    fs.writeFileSync(path.join(output,'workbench.png'),(await win.webContents.capturePage()).toPNG());
    await win.reload();
    await new Promise(r=>win.webContents.once('did-finish-load',r));
    const restored=await win.webContents.executeJavaScript(`(async()=>{for(let i=0;i<100;i++){if(document.querySelectorAll('.wb-workflow-card').length===7)return true;await new Promise(r=>setTimeout(r,100));}return false;})()`);
    if(!restored)throw Error('Refresh lost graph');
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({...result,restored,errors},null,2));
    if(errors.length)throw Error(errors.join('\n'));
    console.log('WORKFLOW_RESULT '+JSON.stringify({...result,restored}));app.exit(0);
  }catch(error){
    fs.writeFileSync(path.join(output,'failure.png'),(await win.webContents.capturePage()).toPNG());
    console.error(error.stack);app.exit(1);
  }
});
