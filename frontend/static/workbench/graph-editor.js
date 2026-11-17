import {node, button} from './dom.js';

export const kinds={implement:'执行',plan:'规划',reduce:'汇总',audit:'审查',decision:'判断',gate:'人工确认'};
export const conditions={always:'下一步',pass:'是 / 通过',fail:'否 / 未通过',loop:'循环继续'};

export function outlets(item, edges=[]) {
  const defaults=['decision','audit','gate'].includes(item.kind) ? ['pass','fail'] : ['always'];
  const outgoing=edges.filter(edge=>edge.from===item.id);
  const names=[...new Set([...defaults,...outgoing.map(edge=>edge.condition || 'always')])];
  return names.flatMap(condition=>{
    const matches=outgoing.filter(edge=>(edge.condition || 'always')===condition);
    const label=item.kind==='decision' ? ({pass:'是',fail:'否'}[condition] || conditions[condition]) : conditions[condition];
    return matches.length>1 ? matches.map((edge,index)=>({condition,edgeId:edge.id,
      label:edge.input_when ? `${edge.input_when.input} = ${edge.input_when.equals}` : `${label} ${index+1}`})) :
      [{condition,edgeId:matches[0]?.id,label:matches[0]?.input_when ? `${matches[0].input_when.input} = ${matches[0].input_when.equals}` : label}];
  });
}

function field(form,label,element,value) {
  const wrapper=node('label','wb-field',label);element.className='wb-input';
  element.setAttribute('aria-label',label);element.value=value ?? '';wrapper.append(element);form.append(wrapper);return element;
}
function select(options,value) {
  const element=node('select');
  for(const [key,label] of Object.entries(options)) {const option=node('option','',label);option.value=key;element.append(option);}
  element.value=value;return element;
}

export function nodeForm(item, detail, submit, cancel) {
  const form=node('form','wb-graph-edit-form');
  form.append(node('strong','',item ? '编辑节点' : '添加节点'));
  if(item) form.append(node('code','wb-node-id',`ID: ${item.id}`));
  const title=field(form,'节点名称',node('input'),item?.label || '');title.required=true;title.maxLength=180;
  const kind=field(form,'节点类型',select(kinds,item?.kind || 'implement'),item?.kind || 'implement');
  const goal=field(form,'节点目标',node('textarea'),detail?.goal || '');goal.rows=3;goal.maxLength=6000;
  const lists={};
  for(const [key,label] of [['inputs','输入（每行一项）'],['outputs','产出（每行一项）'],['requirements','验收要求（每行一项）']]) {
    lists[key]=field(form,label,node('textarea'),(detail?.[key] || []).join('\n'));lists[key].rows=2;
  }
  form.append(node('p','wb-muted','普通出口可连接多个下游。判断、审查和人工确认节点有不同条件的出口。'));
  const save=button('保存节点',()=>{},'wb-button wb-primary');save.type='submit';
  form.append(save,button('取消',cancel));
  form.addEventListener('submit',async event=>{
    event.preventDefault();save.disabled=true;
    const values=Object.fromEntries(Object.entries(lists).map(([key,input])=>[key,input.value.split('\n').map(s=>s.trim()).filter(Boolean)]));
    try {await submit({action:'upsert',kind:'node',id:item?.id,fields:{title:title.value.trim(),body:goal.value,
      data:{node_kind:kind.value,inputs:values.inputs,outputs:values.outputs,acceptance:values.requirements}}});}
    finally {save.disabled=false;}
  });
  return form;
}

export function edgeForm(edge, nodes, submit, cancel) {
  const form=node('form','wb-graph-edit-form');form.append(node('strong','',edge ? '编辑连线' : '添加连线'));
  const options=Object.fromEntries(nodes.map(item=>[item.id,`${item.label} [${item.id}]`]));
  const from=field(form,'上游节点',select(options,edge?.from || nodes[0]?.id),edge?.from || nodes[0]?.id);
  const to=field(form,'下游节点',select(options,edge?.to || nodes[1]?.id),edge?.to || nodes[1]?.id);
  const condition=field(form,'出口条件',select(conditions,edge?.condition || 'always'),edge?.condition || 'always');
  form.append(node('p','wb-muted','同一出口可分别连接多个下游；判断出口根据节点结果选择分支。'));
  const save=button('保存连线',()=>{},'wb-button wb-primary');save.type='submit';form.append(save,button('取消',cancel));
  form.addEventListener('submit',async event=>{
    event.preventDefault();save.disabled=true;
    try {await submit({action:'upsert',kind:'edge',id:edge?.id,fields:{data:{from_node:from.value,to_node:to.value,condition:condition.value}}});}
    finally {save.disabled=false;}
  });
  return form;
}
