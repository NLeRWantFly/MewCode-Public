const assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
const {setup}=require('../workbench_dom.cjs');

(async()=>{
  const document=setup();
  const {renderWorkflowBlocks}=await import(pathToFileURL(path.resolve(__dirname,'../../frontend/static/workbench/workflow-blocks.js')));
  const root=document.createElement('div');document.body.append(root);
  const blocks=[
    {block_id:'source',slot:'input',kind:'text',title:'行测章节',text:'<script>bad()</script>'},
    {block_id:'table',slot:'output',kind:'table',title:'章节目录',columns:['章节','数量'],rows:[['第一节',3]]},
    {block_id:'chart',slot:'output',kind:'chart',title:'题型分布',labels:['A','B'],series:[{name:'题量',values:[2,4]}]},
    {block_id:'graph',slot:'output',kind:'diagram',title:'章节关系',nodes:[{id:'a',label:'材料'},{id:'b',label:'结果'}],edges:[{source:'a',target:'b'}]},
    {block_id:'image',slot:'output',kind:'image',title:'图像',artifact_id:'fixed-image',caption:'来源图'}
  ];
  renderWorkflowBlocks(root,blocks,{scope:{conversation_id:'chat'},task:{id:'task',run_id:'run'}});
  assert.equal(root.querySelectorAll('.wb-io-group').length,2);
  assert.equal(root.querySelectorAll('svg').length,2);
  assert.equal(root.querySelectorAll('table').length,1);
  assert.equal(root.querySelectorAll('script').length,0);
  assert.ok(root.textContent.includes('<script>bad()</script>'));
  assert.ok(root.querySelector('img').src.includes('file_id=fixed-image'));
  assert.ok(root.querySelector('img').src.includes('inline=1'));
  renderWorkflowBlocks(root,blocks,{compact:true});
  assert.equal(root.querySelectorAll('.wb-io-block').length,2);
  renderWorkflowBlocks(root,[{...blocks[2],series:[{name:'bad',values:[NaN]}]}]);
  assert.ok(root.textContent.includes('图表数据不完整'));
  renderWorkflowBlocks(root,[]);
  assert.equal(root.hidden,true);
  console.log('presentation: input/output, table/chart/diagram/image, text escaping, compact view and fallback passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
