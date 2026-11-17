import {node,button} from './dom.js';

const SAVED='mewcode-workflow-connection-v1';
export function mountConnections(aside,{api,openWorkspace,opener}) {
  const panel=node('section','wb-connections'),heading=node('header','wb-connection-heading');
  panel.hidden=true;panel.setAttribute('aria-label','连接的运行');
  const back=button('← 返回本地工作台',()=>close()),title=node('strong','','连接的运行');
  const state=node('span','wb-connection-state'),refresh=button('刷新连接',()=>void load());
  heading.append(back,title,state,refresh);
  const content=node('div','wb-connection-content'),status=node('p','wb-connection-notice');
  status.setAttribute('role','status');panel.append(heading,status,content);aside.append(panel);
  let rows=[],selected='',frame=null,disposed=false,pending=null,background=[],returnFocus=null,listSignature='';
  const saved=()=>{try{return localStorage.getItem(SAVED)||'';}catch{return '';}};
  function remember(id){try{if(id)localStorage.setItem(SAVED,id);else localStorage.removeItem(SAVED);}catch{}}
  function show(){
    openWorkspace();
    if(panel.hidden){
      returnFocus=document.activeElement;
      for(const element of aside.children)if(element!==panel){background.push([element,element.inert]);element.inert=true;}
      panel.hidden=false;back.focus();
    }
  }
  function close(){
    selected='';remember('');frame?.remove();frame=null;panel.hidden=true;
    for(const [element,inert]of background)element.inert=inert;background=[];
    (returnFocus?.isConnected?returnFocus:opener)?.focus();
  }
  function renderList(){
    const focused=document.activeElement?.dataset?.connectionId;
    listSignature=JSON.stringify(rows);
    content.replaceChildren();title.textContent='连接的运行';state.textContent='';
    status.textContent='在客户端查看同一份设计图、节点对话、产物与实时执行状态。';
    if(!rows.length){content.append(node('p','wb-muted','当前没有已登记的工作流连接。'));return;}
    for(const row of rows){
      const card=node('article','wb-connection-card');
      const info=node('div');info.append(node('strong','',row.title),node('p','wb-muted',row.project),
        node('span','',`${row.status_label} · ${row.completed}/${row.nodes} 个节点已完成`));
      const connect=button('打开当前工作流',()=>open(row));connect.disabled=!row.available;
      connect.dataset.connectionId=row.id;
      card.append(info,connect);content.append(card);
      if(row.id===focused&&!connect.disabled)connect.focus();
    }
  }
  function open(row){
    if(!row.available)return;
    const url=new URL(row.url,location.href);
    if(url.origin!==location.origin||url.pathname!=='/workbench/connections/'+row.id||url.search||url.hash||url.username||url.password)return;
    show();selected=row.id;remember(selected);title.textContent=row.title;state.textContent=row.status_label;
    status.textContent='已连接 · 设计与执行实时同步';
    if(frame?.src!==url.href){
      frame?.remove();frame=node('iframe','wb-connected-workflow');frame.title=row.title+' · 实时工作流';
      frame.referrerPolicy='no-referrer';frame.src=url.href;content.replaceChildren(frame);
    }
  }
  async function load(){
    if(disposed||pending)return pending;
    pending=(async()=>{
      try{
        const value=await api.get('/api/workbench/connections');if(disposed)return;
        rows=value.connections||[];
        opener.title=rows.length?`连接的运行 · ${rows.length}`:'连接的运行';
        opener.dataset.count=String(rows.length);
        if(selected){
          const row=rows.find(r=>r.id===selected);
          state.textContent=row?.status_label||'连接暂不可用';
          status.textContent=row?.available?'已连接 · 设计与执行实时同步':'连接暂不可用；保留当前页面，恢复后可继续查看。';
        }else if(!panel.hidden&&listSignature!==JSON.stringify(rows))renderList();
      }catch{
        if(!panel.hidden)status.textContent='暂时无法读取工作流连接，请稍后刷新。';
      }finally{pending=null;}
    })();return pending;
  }
  async function browse(){show();if(selected)return;renderList();await load();}
  async function openCurrent(){
    await load();if(disposed)return;
    const available=rows.filter(row=>row.available);
    if(available.length===1)open(available[0]);else await browse();
  }
  const timer=setInterval(()=>{if(!panel.hidden)void load();},10000);timer?.unref?.();
  void load().then(()=>{const id=saved(),row=rows.find(r=>r.id===id);if(!disposed&&row?.available)open(row);});
  function exportCurrent(){
    const exporter=frame?.contentWindow?.MewWorkbench?.exporter;
    if(!exporter)return false;void exporter.open();return true;
  }
  return {browse,openCurrent,exportCurrent,close,open,load,destroy(){disposed=true;clearInterval(timer);close();panel.remove();}};
}
