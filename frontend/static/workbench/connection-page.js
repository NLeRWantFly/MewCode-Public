const binding=JSON.parse(document.getElementById('workflow-connection').textContent);
const parentHost=window.parent===window?null:window.parent;
function syncTheme(){
  const theme=parentHost?.document.documentElement.getAttribute('data-theme')||'light';
  document.documentElement.setAttribute('data-theme',theme);
  window.dispatchEvent(new CustomEvent('mewcode:theme'));
}
window.MewWorkbenchHost={context:()=>binding,apiBase:binding.apiBase,toggleTheme:()=>{
  parentHost?.MewWorkbenchHost?.toggleTheme?.();syncTheme();
}};
syncTheme();parentHost?.addEventListener('mewcode:theme',syncTheme);
window.addEventListener('pagehide',()=>parentHost?.removeEventListener('mewcode:theme',syncTheme),{once:true});
await import('./index.js?v=20261111-input-wait');
window.MewWorkbench.store.selectTask({id:'',run_id:binding.run_id});
window.MewWorkbench.open();
