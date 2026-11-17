const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {setup}=require('./workbench_dom.cjs');
const script=fs.readFileSync(path.resolve(__dirname,'../frontend/static/fast-mode.js'),'utf8');
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};

function fixture(fetcher){
  const doc=setup();doc.querySelectorAll=selector=>doc.body.querySelectorAll(selector);
  const root=doc.createElement('div');doc.body.append(root);
  const window={fetch:fetcher};vm.runInNewContext(script,{window});
  let status={conversation_id:'c',provider_key:'relay',model:'gpt-6.1-sol',effort:'max',fast_mode:false,fast_mode_available:true};
  const updates=[];
  const control=window.MewFastMode.mount(root,{getStatus:()=>status,onChange:value=>{updates.push(value);status={...status,...value};}});
  return {root,control,updates,toggle:root.querySelector('button'),get status(){return status;},set status(value){status=value;}};
}

(async()=>{
  let cases=0;
  {
    const calls=[];const h=fixture(async(path,options)=>{
      const body=JSON.parse(options.body);calls.push({path,headers:options.headers,body});
      return {ok:true,json:async()=>({status:{fast_mode:body.enabled,service_tier:body.enabled?'fast':'default'}})};
    });
    assert.equal(h.toggle.getAttribute('aria-checked'),'false');
    await h.toggle.click();assert.equal(h.toggle.getAttribute('aria-checked'),'true');
    assert.equal(h.status.effort,'max');assert.equal(h.status.model,'gpt-6.1-sol');
    assert.equal(calls[0].path,'/api/settings/fast-mode');assert.equal(calls[0].headers['X-Mew-Fast-Mode'],'1');
    assert.deepEqual(calls[0].body,{conversation_id:'c',provider_key:'relay',model_id:'gpt-6.1-sol',enabled:true});
    await h.toggle.click();assert.equal(calls[1].body.enabled,false);assert.equal(h.status.service_tier,'default');cases++;
  }
  {
    const pending=deferred();let calls=0;const h=fixture(()=>{calls++;return pending.promise;});
    const first=h.toggle.click();assert.equal(h.toggle.disabled,true);await h.toggle.click();assert.equal(calls,1);
    h.status={...h.status,conversation_id:'next',fast_mode:false};h.control.update();
    pending.resolve({ok:true,json:async()=>({status:{fast_mode:true}})});await first;
    assert.equal(h.updates.length,0);assert.equal(h.toggle.getAttribute('aria-checked'),'false');assert.equal(h.toggle.disabled,false);cases++;
  }
  {
    const h=fixture(async()=>({ok:false,json:async()=>({error:'service_tier is unavailable'})}));
    await h.toggle.click();assert.equal(h.toggle.disabled,false);assert.equal(h.updates.length,0);
    assert.equal(h.toggle.getAttribute('aria-checked'),'false');assert.match(h.root.textContent,/service_tier is unavailable/);cases++;
  }
  {
    let calls=0;const h=fixture(async()=>{calls++;throw Error('must not send');});
    h.status={...h.status,fast_mode_available:false};h.control.update();await h.toggle.click();
    assert.equal(calls,0);assert.equal(h.toggle.disabled,true);cases++;
  }
  {
    const doc=setup();doc.querySelectorAll=selector=>doc.body.querySelectorAll(selector);
    const root=doc.createElement('div');doc.body.append(root);let status={conversation_id:'c',provider_key:'p',model:'gpt-6.1-sol',fast_mode_available:true};
    const window={fetch:async()=>({ok:true,json:async()=>({status:{fast_mode:true}})})};vm.runInNewContext(script,{window});
    function render(){window.MewFastMode.mount(root,{getStatus:()=>status,onChange:value=>{status={...status,...value};render();}});}
    render();await root.querySelector('button').click();
    assert.equal(root.querySelector('button').disabled,false,'picker rerender does not leave a replacement control pending');
    assert.equal(root.querySelector('button').getAttribute('aria-checked'),'true');cases++;
  }
  console.log(`fast mode: ${cases} cases passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
