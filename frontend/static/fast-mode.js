(function (global) {
  'use strict';
  const pending = new Set();
  const route = status => JSON.stringify([status.conversation_id, status.provider_key, status.model]);

  function mount(container, {getStatus, onChange, fetcher = global.fetch.bind(global)}) {
    const doc=container.ownerDocument,copy=doc.createElement('div'),title=doc.createElement('strong');
    const description=doc.createElement('small'),toggle=doc.createElement('button'),message=doc.createElement('small');
    container.classList.add('runtime-review-model');container.classList.add('runtime-fast-mode');
    description.textContent='向当前服务商请求更快处理，可能使用更多额度或费用。对该模型后续请求生效，保留推理强度。';
    copy.append(title,description);toggle.type='button';toggle.setAttribute('role','switch');
    toggle.setAttribute('aria-label','Fast 模式');message.setAttribute('role','status');
    container.replaceChildren(copy,toggle,message);
    let feedback='',feedbackOwner='';
    function update() {
      const status=getStatus(),owner=route(status),available=!!status.fast_mode_available;
      title.textContent='Fast · '+(status.model||'未选择模型');
      toggle.textContent=pending.has(owner)?'正在保存…':status.fast_mode?'Fast 已开启':'开启 Fast';
      toggle.setAttribute('aria-checked',String(!!status.fast_mode));
      toggle.disabled=!available||!status.conversation_id||pending.has(owner);
      message.textContent=feedbackOwner===owner?feedback:available?'':'当前模型没有 Fast 设置';
      message.hidden=!message.textContent;
    }
    toggle.addEventListener('click',async()=>{
      const status={...getStatus()},owner=route(status);if(toggle.disabled||pending.has(owner))return;
      pending.add(owner);feedback='';update();
      try {
        const response=await fetcher('/api/settings/fast-mode',{
          method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Mew-Fast-Mode':'1'},
          body:JSON.stringify({conversation_id:status.conversation_id,provider_key:status.provider_key,
            model_id:status.model,enabled:!status.fast_mode})});
        const value=await response.json();if(!response.ok||value.error)throw Error(value.error||'Fast 设置未保存');
        if(route(getStatus())!==owner)return;
        onChange(value.status);
        feedback='已保存，后续请求生效。';feedbackOwner=owner;
      } catch(error) {
        if(route(getStatus())===owner){feedback=error.message||'Fast 设置未保存';feedbackOwner=owner;}
      } finally {
        pending.delete(owner);update();
        for(const element of doc.querySelectorAll?.('.runtime-fast-mode')||[])element.fastModeControl?.update();
      }
    });
    const control={update};container.fastModeControl=control;update();return control;
  }
  global.MewFastMode={mount};
})(window);
