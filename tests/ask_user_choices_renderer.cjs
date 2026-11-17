// Exercise the real prompt renderer, answer collection and wait status in an offline DOM.
const fs = require('node:fs'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../frontend/static/app.js'), 'utf8').replace(/\r\n/g, '\n');
function helper(name) {
  const start = source.indexOf('  function ' + name + '('), end = source.indexOf('\n  }', start);
  if (start < 0 || end < 0) throw new Error('Missing prompt helper ' + name);
  return source.slice(start, end + 4);
}
module.exports = `(()=>{
 const assert=(ok,msg)=>{if(!ok)throw new Error(msg);};
 const $=id=>document.getElementById(id),humanOnCallPanel=$('chat-human-oncall');
 let currentAskUser=null,currentHumanOnCall=null;
 const humanOnCallDrafts=new Map(),askUserDrafts=new Map();
 const activeConversationId=()=> 'c',recoveryCases=new Map(),sent=[],send=msg=>sent.push(msg),addErrorMsg=msg=>{throw new Error(msg);};
 const state={status:'running',attempt:1,waits:new Map(),cards:new Map(),timeline:document.createElement('div')};
 state.timeline.innerHTML='<strong class="expert-run-status"></strong>';
 const card=document.createElement('div');card.className='expert-stage is-running';card.dataset.attempt='1';
 const ensureExpertTimelineV2=()=>state,ensureExpertStageV2=()=>({key:'N01'}),showGateCountdown=()=>{};
 state.cards.set('N01',card);
  ${['askUserOptionLabel','askUserOptionValue','askUserPresentation','setHumanOnCallFeedback',
     'humanOnCallDraftKey','rememberHumanOnCallDraft','forgetHumanOnCallDraft','savedHumanOnCallDraft',
     'askUserDraftKey','rememberAskUserDraft','forgetAskUserDraft','savedAskUserDraft','collectAskUserNotes',
    'clearHumanOnCallAckTimer','resetHumanOnCallControls','setHumanOnCallSubmitting','setHumanOnCallPanelMode',
    'selectHumanOnCallChoice','showHumanOnCall','answerHumanOnCall',
     'updateAskUserSubmitState','promptVersion','showAskUser','collectAskUserAnswers','choiceInputValue','answerAskUser',
    'pendingRecoveryCases','expertRunStatusLabel','syncExpertHumanWaits','renderExpertRoleWaitV2'].map(helper).join('\n')}
 const choices={type:'radio',name:'direction',title:'选择分类方式',context:'节点：调查四个PDF并制定初始方案',
   message:'四本书采用哪种分类方式？',allow_other:true,
   options:[{id:'book',label:'按书目分类',description:'保留每本书的章节树及来源。',recommended:true},
            {value:'topic',label:'按专题分类',description:'按内容主题合并，保留书目来源。'}],
   details:'已尝试：核验文件、检查分类规则。\\n'+'历史诊断记录；'.repeat(160)};
 const show=(q,runId='')=>showAskUser({request_id:'request',conversation_id:'c',...(runId?{run_id:runId}:{}),questions:[q]});
 show(choices);
 assert($('human-oncall-question').textContent==='选择分类方式','Long question became heading');
 assert(humanOnCallPanel.textContent.split(choices.message).length===2,'Question duplicated');
 assert($('ask-user-questions').querySelectorAll('.human-oncall-option').length===3,'Choices/custom choice absent');
 assert(humanOnCallPanel.querySelector('.human-oncall-recommended').textContent==='建议方案','Recommendation lost');
 assert(!humanOnCallPanel.querySelector('details').open,'Diagnostics expanded');
 assert($('human-oncall-submit').disabled,'Unchosen option was auto-approved');
 const choiceNote=humanOnCallPanel.querySelector('.ask-user-note-input');
 assert(!choiceNote.closest('details').open,'Optional note should be collapsed for a choice question');
 choiceNote.value='先保留现有证据';choiceNote.dispatchEvent(new Event('input'));
 answerAskUser(false);assert(sent.length===0,'Empty answer submitted');
 humanOnCallPanel.querySelector('[data-value="book"]').click();
 assert(collectAskUserAnswers().direction==='book','Stable ID lost');
 answerAskUser(false);assert(sent[0].answers.direction==='book'&&sent[0].notes.direction==='先保留现有证据','Canonical answer or note not sent');
 show(choices);humanOnCallPanel.querySelector('.ask-user-note-input').value='回放后仍保留的线索';
 humanOnCallPanel.querySelector('.ask-user-note-input').dispatchEvent(new Event('input'));
 humanOnCallPanel.querySelector('[data-value="topic"]').click();
 show(choices);
 assert(humanOnCallPanel.querySelector('[data-value="topic"]').getAttribute('aria-checked')==='true'
   &&humanOnCallPanel.querySelector('.ask-user-note-input').value==='回放后仍保留的线索','Same-request choice or note draft lost');
 humanOnCallPanel.querySelector('[data-other="true"]').click();
 assert($('human-oncall-submit').disabled,'Empty custom choice accepted');
 const custom=humanOnCallPanel.querySelector('.ask-user-custom');custom.value='保留书目，再增加专题标签';custom.dispatchEvent(new Event('input'));
 assert(!custom.classList.contains('hidden')&&!$('human-oncall-submit').disabled,'Custom answer unavailable');
 assert(collectAskUserAnswers().direction===custom.value,'Custom answer lost');
 humanOnCallPanel.querySelector('[data-value="topic"]').click();
 assert(custom.classList.contains('hidden')&&collectAskUserAnswers().direction==='topic','Old custom value leaked into selected choice');
 humanOnCallPanel.querySelector('[data-other="true"]').click();
 assert(custom.value==='保留书目，再增加专题标签','Custom draft was discarded when switching choices');
 show(choices,'run-a');
 humanOnCallPanel.querySelector('[data-value="topic"]').click();
 humanOnCallPanel.querySelector('.ask-user-note-input').value='run-a canonical note';
 humanOnCallPanel.querySelector('.ask-user-note-input').dispatchEvent(new Event('input'));
 show(choices,'run-b');
 assert(humanOnCallPanel.querySelector('[data-value="topic"]').getAttribute('aria-checked')!=='true'
   &&humanOnCallPanel.querySelector('.ask-user-note-input').value===''&&!humanOnCallPanel.querySelector('.ask-user-custom').value,
   'AskUser draft leaked canonical choice or note across runs');
 show(choices,'run-a');
 assert(humanOnCallPanel.querySelector('[data-value="topic"]').getAttribute('aria-checked')==='true'
   &&humanOnCallPanel.querySelector('.ask-user-note-input').value==='run-a canonical note',
   'AskUser same-run canonical choice or note was not replayed');
 humanOnCallPanel.querySelector('[data-other="true"]').click();
 const scopedCustom=humanOnCallPanel.querySelector('.ask-user-custom');
 scopedCustom.value='run-a other draft';scopedCustom.dispatchEvent(new Event('input'));
 show(choices,'run-b');
 assert(humanOnCallPanel.querySelector('[data-other="true"]').getAttribute('aria-checked')!=='true'
   &&humanOnCallPanel.querySelector('.ask-user-custom').value==='',
   'AskUser Other draft leaked across runs');
 show(choices,'run-a');
 assert(humanOnCallPanel.querySelector('[data-other="true"]').getAttribute('aria-checked')==='true'
   &&humanOnCallPanel.querySelector('.ask-user-custom').value==='run-a other draft',
   'AskUser same-run Other draft was not replayed');
 show({...choices,type:'text'});assert(humanOnCallPanel.querySelector('[data-value="book"]'),'Options suppressed for legacy text type');
 show({...choices,type:'checkbox'});
 humanOnCallPanel.querySelector('[data-value="book"]').click();humanOnCallPanel.querySelector('[data-other="true"]').click();
 assert($('human-oncall-submit').disabled,'Checkbox Other was optional beside a standard choice');
 const checkboxCustom=humanOnCallPanel.querySelector('.ask-user-custom');checkboxCustom.value='附加方案';checkboxCustom.dispatchEvent(new Event('input'));
 assert(!$('human-oncall-submit').disabled,'Filled checkbox Other stayed blocked');answerAskUser(true);
 show({...choices,type:'checkbox',allow_other:false});
 humanOnCallPanel.querySelector('[data-value="book"]').click();humanOnCallPanel.querySelector('[data-value="topic"]').click();
 assert(collectAskUserAnswers().direction==='book, topic','Multiple selections lost');
 show({type:'radio',name:'missing',message:'请提供材料',options:[]});
 const input=humanOnCallPanel.querySelector('.ask-user-text');input.value='材料路径';input.dispatchEvent(new Event('input'));
 assert(collectAskUserAnswers().missing==='材料路径','Empty option list lost text fallback');
 show({type:'radio',name:'legacy',message:'采用哪个环境？',options:['staging','prod']});
 humanOnCallPanel.querySelector('[data-value="staging"]').click();assert(collectAskUserAnswers().legacy==='staging','Legacy strings broken');
 show({type:'text',name:'direction',message:'节点「调查四个PDF」需要具体决定：选择分类规则。\\n已尝试：'+ '重复恢复记录；'.repeat(100)});
 assert($('human-oncall-question').textContent==='需要你确认','Legacy giant title remains');
 assert(humanOnCallPanel.querySelector('.ask-user-item-label').textContent==='选择分类规则。','Legacy history not separated');
 assert(humanOnCallPanel.querySelector('.ask-user-context').textContent==='节点：调查四个PDF','Node context lost');
 assert(!humanOnCallPanel.querySelector('details').open,'Legacy history not collapsed');
 show({...choices,message:'<img src=x onerror=alert(1)>',options:[{value:'x',label:'<script>bad</script>'}]});
 assert(!humanOnCallPanel.querySelector('img,script'),'Prompt strings executed as HTML');
 answerAskUser(true);assert(Object.keys(sent.at(-1).answers).length===0,'Cancel fabricated answer');
 renderExpertRoleWaitV2({run_id:'r',node_id:'N01',kind:'ask_user',wait_id:'q',started_at:100,active:true});
 assert(expertRunStatusLabel(state)==='等待你的回答','Human wait displayed as running');
 const other=document.createElement('div');other.className='is-running';other.dataset.attempt='1';state.cards.set('N02',other);
 assert(expertRunStatusLabel(state)==='等待你的回答 · 其他节点执行中','Independent work status hidden');
 other.dataset.attempt='0';assert(expertRunStatusLabel(state)==='等待你的回答','Historical work counted as active');
 state.status='paused';assert(expertRunStatusLabel(state)==='已暂停','Wait overwrote terminal status');state.status='running';
 syncExpertHumanWaits(state,{node_states:{N01:{}}});assert(expertRunStatusLabel(state)==='执行中','Reconnect kept obsolete wait');
 renderExpertRoleWaitV2({run_id:'r',node_id:'N01',kind:'permission',wait_id:'p',started_at:110,active:true});
 assert(expertRunStatusLabel(state)==='等待授权','Permission wait hidden');
 renderExpertRoleWaitV2({run_id:'r',node_id:'N01',kind:'permission',wait_id:'p',started_at:110,completed_at:120,active:false});
 assert(expertRunStatusLabel(state)==='执行中','Completed wait still shown');
 state.runId='r';card.dataset.nodeId='N01';other.dataset.nodeId='N02';
 recoveryCases.set('case',{run_id:'r',conversation_id:'c',node_id:'N01',case:{status:'reported',pending:{kind:'incident',options:[{value:'retry'}]}}});
 assert(expertRunStatusLabel(state)==='等待你的选择 · 自动修复已停止','Reported case looks running');
 other.dataset.attempt='1';
 assert(expertRunStatusLabel(state)==='等待你的选择 · 其他节点执行中','Case wait hides independent execution');
 state.status='completed';assert(expertRunStatusLabel(state)==='已完成','Case overrides completed run');
 state.status='running';recoveryCases.clear();other.dataset.attempt='0';
 const scopedHuman={name:'scope',title:'选择运行范围',question:'选择当前运行要处理的范围。',allowOther:true,
   options:[{value:'canonical',label:'固定范围'},{value:'fallback',label:'备用范围'}]};
 const humanScopeMsg=runId=>({request_id:'human-scope',conversation_id:'c',run_id:runId,questions:[scopedHuman]});
 showHumanOnCall(humanScopeMsg('human-run-a'));
 humanOnCallPanel.querySelector('[data-value="__other__"]').click();
 $('human-oncall-other').value='human-run-a other';$('human-oncall-note').value='human-run-a note';
 answerHumanOnCall(false);
 showHumanOnCall(humanScopeMsg('human-run-b'));
 assert(!humanOnCallPanel.querySelector('.human-oncall-option[aria-checked="true"]')
   &&$('human-oncall-other').value===''&&$('human-oncall-note').value==='',
   'HumanOnCall draft leaked canonical choice, Other, or note across runs');
 showHumanOnCall(humanScopeMsg('human-run-a'));
 assert(humanOnCallPanel.querySelector('[data-value="__other__"]').getAttribute('aria-checked')==='true'
   &&$('human-oncall-other').value==='human-run-a other'&&$('human-oncall-note').value==='human-run-a note',
   'HumanOnCall same-run Other or note was not replayed');
 const ordinary={name:'ordinary',title:'选择核验路径',options:[{value:'host',label:'宿主核验',recommended:true},{value:'model',label:'模型复核'}]};
 const beforeOrdinary=sent.length;showHumanOnCall({request_id:'ordinary',conversation_id:'c',questions:[ordinary]});
 assert(!humanOnCallPanel.querySelector('.human-oncall-option[aria-checked="true"]')&&$('human-oncall-submit').disabled,'Ordinary recommendation was auto-selected');
 $('human-oncall-note').value='仅补充背景';$('human-oncall-note').dispatchEvent(new Event('input'));answerHumanOnCall(false);
 assert(sent.length===beforeOrdinary,'Note alone submitted HumanOnCall choice');
 humanOnCallPanel.querySelector('[data-value="host"]').click();answerHumanOnCall(false);
 assert(sent.at(-1).answers.ordinary==='host'&&sent.at(-1).notes.ordinary==='仅补充背景','HumanOnCall note or canonical answer lost');
 const gate={name:'choice',title:'确认费用与内容外发的方案',question:'核对计划及范围后决定是否继续。',
   context:'已取得的阶段结果\\n四个PDF共1073页；SQLite主库。\\n\\n确认后准备进入的环节\\n16页代表试点；deepseek-flash / max。\\n\\n本次参数与待确认范围\\n现有额度，用完暂停；必要原页和图块；候选目录。',
   options:[{value:'continue',label:'批准这版方案'},{value:'pause',label:'暂不执行'}],
   allowOther:true,requires_explicit_selection:true};
 showHumanOnCall({request_id:'gate',conversation_id:'c',questions:[gate]});
 assert($('human-oncall-question').textContent===gate.title,'Gate title is generic');
 assert($('human-oncall-context').textContent.includes('1073')&&$('human-oncall-context').textContent.includes('deepseek-flash / max'),'Gate plan or model absent');
 assert(!humanOnCallPanel.querySelector('.human-oncall-option[aria-checked="true"]')&&$('human-oncall-submit').disabled,'Gate auto-selected approval');
 const beforeGate=sent.length;answerHumanOnCall(false);assert(sent.length===beforeGate,'No selection was submitted as consent');
 humanOnCallPanel.querySelector('[data-value="__other__"]').click();
 $('human-oncall-other').value='你目前的计划是什么，先告诉我';
 answerHumanOnCall(false);
 assert(sent.at(-1).answers.choice==='你目前的计划是什么，先告诉我','Question became approval');
 showAskUser({request_id:'text-exception',conversation_id:'c',choice_bundle:{id:'text-bundle',version:'v1'},
   questions:[{name:'output',type:'text',input_mode:'text',message:'请提供保存位置',
     options:[{value:'manual',label:'其他填写'}]}]});
 const textAnswer=humanOnCallPanel.querySelector('.ask-user-text');
 assert(textAnswer&&$('ask-user-questions').querySelectorAll('.human-oncall-option').length===0,'Text exception still requires choosing an input action');
 textAnswer.value='defer';textAnswer.dispatchEvent(new Event('input'));answerAskUser(false);
 assert(sent.at(-1).answers.output.custom==='defer','Free input was mistaken for a prepared action token');
 window.showGateContextFixture=()=>showHumanOnCall({request_id:'gate',conversation_id:'c',questions:[gate]});
 window.showAskUserFixture=()=>show(choices);window.showAskUserFixture();
 return {choices:true,stableIds:true,custom:true,legacy:true,waitStatus:true};
})()`;
