import {node} from './dom.js';
const identity=ref=>ref?JSON.stringify([ref.provider_key,ref.model_id]):'';

export function modelPicker(container,label,models,initial,{optional=false}={}) {
  const wrap=node('label','wb-model-picker',label), select=node('select','wb-input'), effort=node('select','wb-input');
  select.setAttribute('aria-label',label+'模型');effort.setAttribute('aria-label',label+'推理强度');
  const refs=new Map(models.map(row=>[identity(row),row]));
  if(initial&&!refs.has(identity(initial)))refs.set(identity(initial),{...initial,unavailable:true});
  const empty=node('option','',optional?'未配置':'请选择模型');empty.value='';select.append(empty);
  for(const [key,ref] of refs){const option=node('option','',`${ref.provider_name||ref.provider_key} · ${ref.name||ref.model_id}${ref.unavailable?'（当前不可用）':''}`);option.value=key;select.append(option);}
  select.value=identity(initial);let selectedEffort=initial?.effort||'';
  function render(){const ref=refs.get(select.value);const values=['',...(ref?.efforts||[])];if(selectedEffort&&!values.includes(selectedEffort))values.push(selectedEffort);
    effort.replaceChildren(...values.map(value=>{const option=node('option','',value||'自动');option.value=value;return option;}));effort.value=selectedEffort;effort.disabled=!ref;}
  select.addEventListener('change',()=>{selectedEffort='';render();});effort.addEventListener('change',()=>selectedEffort=effort.value);render();
  wrap.append(select,effort);container.append(wrap);
  return {get(){const ref=refs.get(select.value);if(!ref){if(optional)return null;throw Error('请选择'+label+'模型');}return {provider_key:ref.provider_key,model_id:ref.model_id,effort:effort.value};},disable(value){select.disabled=value;effort.disabled=value||!select.value;}};
}
