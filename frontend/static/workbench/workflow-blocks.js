import {node} from './dom.js';
import {hostPath} from './api.js';

const COLORS=['var(--wb-accent,#2488c7)','var(--wb-pass,#3f9c70)','#a878ca','#d69a43'];
const finite=value=>typeof value==='number'&&Number.isFinite(value);
function svg(tag,attributes={},text=''){
  const item=document.createElementNS('http://www.w3.org/2000/svg',tag);
  for(const [key,value]of Object.entries(attributes))item.setAttribute(key,String(value));
  if(text)item.textContent=text;
  return item;
}
function chart(block){
  const labels=block.labels||[],series=block.series||[];
  if(!labels.length||!series.length||series.some(row=>row.values?.length!==labels.length||row.values.some(value=>!finite(value))))throw new Error('图表数据不完整');
  const picture=svg('svg',{viewBox:'0 0 420 180',role:'img','aria-label':block.title});
  const values=series.flatMap(row=>row.values),scale=Math.max(1,...values.map(value=>Math.abs(value)));
  const low=Math.min(0,...values.map(value=>value/scale)),high=Math.max(0,...values.map(value=>value/scale));
  const range=high-low||1,y=value=>145-(value/scale-low)/range*115,zero=y(0);
  picture.append(svg('line',{x1:24,y1:zero,x2:410,y2:zero,stroke:'currentColor',opacity:'.35'}));
  const band=380/labels.length;
  for(const [index,row]of series.entries()){
    const color=COLORS[index%COLORS.length];
    if(block.chart_type==='line'){
      const points=row.values.map((value,i)=>`${24+(i+.5)*band},${y(value)}`).join(' ');
      picture.append(svg('polyline',{points,fill:'none',stroke:color,'stroke-width':2}));
    }else for(const [i,value]of row.values.entries()){
      const width=band*.8/series.length;
      const bar=svg('rect',{x:24+i*band+band*.1+index*width,y:Math.min(zero,y(value)),width:Math.max(.2,width-1),
        height:Math.max(.5,Math.abs(y(value)-zero)),fill:color});
      bar.append(svg('title',{},`${row.name} · ${labels[i]}：${value}`));picture.append(bar);
    }
    picture.append(svg('text',{x:25+index*110,y:14,fill:color,'font-size':11},row.name));
  }
  for(const [index,label]of labels.entries())if(index%Math.max(1,Math.ceil(labels.length/8))===0)
    picture.append(svg('text',{x:24+(index+.5)*band,y:166,'text-anchor':'middle',fill:'currentColor','font-size':10},String(label).slice(0,12)));
  return picture;
}
function diagram(block){
  const nodes=block.nodes||[],edges=block.edges||[];
  if(!nodes.length)throw new Error('关系图没有节点');
  const height=Math.max(100,Math.ceil(nodes.length/3)*76),picture=svg('svg',{viewBox:`0 0 420 ${height}`,role:'img','aria-label':block.title});
  const positions=new Map(nodes.map((value,index)=>[value.id,{x:8+(index%3)*140,y:12+Math.floor(index/3)*76}]));
  for(const edge of edges){
    const from=positions.get(edge.source),to=positions.get(edge.target);if(!from||!to)continue;
    const x1=from.x+62,y1=from.y+22,x2=to.x+62,y2=to.y+22;
    picture.append(svg('path',{d:`M ${x1} ${y1} L ${x2} ${y2}`,fill:'none',stroke:'var(--wb-wire,#8d7aa8)','stroke-width':1.5}));
    if(edge.label)picture.append(svg('text',{x:(x1+x2)/2,y:(y1+y2)/2-5,'text-anchor':'middle',fill:'currentColor','font-size':10},edge.label));
  }
  for(const value of nodes){
    const point=positions.get(value.id),group=svg('g');
    group.append(svg('rect',{x:point.x,y:point.y,width:124,height:44,rx:6,fill:'var(--wb-surface,#fff)',stroke:'var(--wb-line,#bbb)'}));
    const text=svg('text',{x:point.x+62,y:point.y+26,'text-anchor':'middle',fill:'currentColor','font-size':12},String(value.label).slice(0,16));
    text.append(svg('title',{},value.label));group.append(text);picture.append(group);
  }
  return picture;
}

export function renderWorkflowBlocks(container,blocks=[],{scope={},task={},compact=false}={}){
  container.replaceChildren();container.classList.add('wb-workflow-blocks');container.hidden=!blocks.length;
  for(const [slot,label]of [['input','输入'],['output','输出']]){
    const selected=blocks.filter(block=>block.slot===slot);if(!selected.length)continue;
    const group=node('section','wb-io-group');group.dataset.slot=slot;
    group.append(node('div','wb-io-label',label));
    for(const block of compact?selected.slice(-1):selected){
      const item=node('article','wb-io-block');item.dataset.kind=block.kind;item.dataset.blockId=block.block_id;
      item.append(node('strong','wb-io-title',block.title));
      try{
        if(block.kind==='text')item.append(node('p','wb-io-text',block.text||''));
        else if(block.kind==='table'){
          const table=node('table'),head=node('thead'),heading=node('tr'),body=node('tbody');
          for(const column of block.columns||[])heading.append(node('th','',String(column)));
          head.append(heading);
          for(const row of (block.rows||[]).slice(0,compact?3:100)){
            const line=node('tr');for(const cell of row)line.append(node('td','',cell==null?'':String(cell)));body.append(line);
          }
          table.append(head,body);item.append(table);
        }else if(block.kind==='chart')item.append(chart(block));
        else if(block.kind==='diagram')item.append(diagram(block));
        else if(block.kind==='image'){
          const image=node('img');image.alt=block.caption||block.title;image.loading='lazy';
          image.src=hostPath('/api/workbench/artifact')+'?'+new URLSearchParams({...scope,run_id:task.run_id,
            task_id:task.id,file_id:block.artifact_id,inline:'1'});item.append(image);
        }else item.append(node('p','wb-muted','该表达可在节点记录中查看。'));
        if(block.caption)item.append(node('p','wb-io-caption',block.caption));
      }catch(error){item.append(node('p','wb-muted',error.message||'表达暂时无法显示，业务执行继续。'));}
      group.append(item);
    }
    container.append(group);
  }
}
