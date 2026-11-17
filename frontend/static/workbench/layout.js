// Keep the existing conversation, composer and handlers beside the canvas.
export function mountLayout({aside, toggle, close, separator, backdrop, chatSlot, window: win = window}) {
  const abort = new AbortController(), signal = abort.signal;
  const conversation=document.getElementById('conversation-pane');
  const home=conversation?.parentNode, next=conversation?.nextSibling;
  let opened = false, opener = null, background = [];
  const resize = document.createElement('div');
  resize.className='wb-chat-resize';resize.tabIndex=0;
  resize.setAttribute('role','separator');resize.setAttribute('aria-orientation','vertical');
  resize.setAttribute('aria-label','调整对话宽度');
  chatSlot?.append(resize);
  let drag=null;
  function width(value, save=true) {
    const max=Math.max(280,Math.min(900,win.innerWidth-340));
    const next=Math.round(Math.max(280,Math.min(max,value)));
    chatSlot?.style.setProperty('--wb-chat-width',next+'px');
    resize.setAttribute('aria-valuemin','280');resize.setAttribute('aria-valuemax',String(max));resize.setAttribute('aria-valuenow',String(next));
    if(save) try {win.localStorage.setItem('mewcode-workbench-chat-width',String(next));} catch {}
  }
  try {const saved=Number(win.localStorage.getItem('mewcode-workbench-chat-width'));if(saved) width(saved,false);} catch {}
  resize.addEventListener('pointerdown',event=>{
    if(event.button!==0) return;
    drag={x:event.clientX,width:chatSlot.getBoundingClientRect().width};
    resize.setPointerCapture(event.pointerId);event.preventDefault();
  },{signal});
  resize.addEventListener('pointermove',event=>{if(drag) width(drag.width+event.clientX-drag.x);},{signal});
  for(const name of ['pointerup','pointercancel','lostpointercapture']) resize.addEventListener(name,()=>{drag=null;},{signal});
  resize.addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    width(event.key==='Home'?280:event.key==='End'?900:chatSlot.getBoundingClientRect().width+(event.key==='ArrowRight'?24:-24));
  },{signal});
  win.addEventListener('resize',()=>{if(opened && chatSlot && !chatSlot.hidden && win.innerWidth>900) width(chatSlot.getBoundingClientRect().width,false);},{signal});
  aside.setAttribute('role','dialog'); aside.setAttribute('aria-modal','true');
  aside.dataset.workspace = 'true';
  function setOpen(value, options) {
    const change = () => updateOpen(value, options);
    return win.MewChatViewport ? win.MewChatViewport.preserve(change) : change();
  }
  function updateOpen(value, {focus = true} = {}) {
    if (value && !opened) opener = document.activeElement;
    opened = value; aside.hidden = !value; separator.hidden = backdrop.hidden = true;
    if(value && chatSlot && win.innerWidth>900) width(chatSlot.getBoundingClientRect().width || 360,false);
    if (chatSlot && conversation) {
      if (value && conversation.parentNode!==chatSlot) chatSlot.append(conversation);
      else if (!value && home && conversation.parentNode===chatSlot) home.insertBefore(conversation,next || null);
    }
    toggle.setAttribute('aria-expanded',String(value));
    toggle.setAttribute('aria-label',value ? '返回对话' : '打开编排工作台');
    if (value && !background.length) {
      for (const id of [chatSlot ? '' : 'conversation-pane','topbar','drawer']) {
        if (!id) continue;
        const element = document.getElementById(id);
        if (element) {background.push([element,element.inert]);element.inert=true;}
      }
    } else if (!value) {
      for (const [element,inert] of background) element.inert=inert;
      background=[];
    }
    if (focus && value) close.focus({preventScroll: true});
    else if (focus) (opener?.isConnected ? opener : toggle).focus({preventScroll: true});
  }
  toggle.addEventListener('click',()=>setOpen(!opened),{signal});
  close.addEventListener('click',()=>setOpen(false),{signal});
  win.addEventListener('keydown',event=>{
    if (!opened || event.defaultPrevented) return;
    if (event.key==='Escape') {event.preventDefault();setOpen(false);}
    if (event.key==='Tab') {
      const all = [...aside.querySelectorAll('button,select,input,textarea,a[href],[tabindex="0"],summary,iframe')]
        .filter(element=>!element.disabled && !element.closest('[inert]') && element.getClientRects().length);
      const first=all[0], last=all.at(-1), active=document.activeElement;
      // Existing chat menus and dialogs can portal outside the workspace.
      if (active!==document.body && !aside.contains(active) && !background.some(([element])=>element.contains(active))) return;
      if (event.shiftKey && (active===first || !aside.contains(active))) {event.preventDefault();last?.focus();}
      else if (!event.shiftKey && (active===last || !aside.contains(active))) {event.preventDefault();first?.focus();}
    }
  },{signal});
  setOpen(false,{focus:false});
  return {open:()=>setOpen(true),close:()=>setOpen(false),isOpen:()=>opened,
    destroy() {setOpen(false,{focus:false});abort.abort();resize.remove();}};
}
