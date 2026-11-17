"""Real collaborative waits must distinguish display choice IDs from values."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from mewcode.agent import AskUserRequest
from mewcode.experts.waits import wait_for_interaction
from mewcode.orchestration import ask_gateway
from mewcode.orchestration.interaction_choices import project_questions
from tests.test_collaboration_mode import RUN_ID, store_for

ALLOW = '授权此操作一次并复审'
DENY = '不允许此操作'


def inputs():
    questions = [dict(name='ref', message='是否允许此具体操作？', type='radio', options=[ALLOW,DENY])]
    bundle = dict(id='frozen-bundle',version='v1',status='ready',questions=[dict(name='ref',
        allowed_values=[ALLOW,DENY], choices=[
            dict(id='ui-allow-17',kind='value',label=ALLOW,value=ALLOW),
            dict(id='ui-deny-28',kind='value',label=DENY,value=DENY)])])
    return questions,bundle


@pytest.mark.asyncio
@pytest.mark.parametrize('choice,value',[('ui-deny-28',DENY),('ui-allow-17',ALLOW)])
async def test_prepared_choice_persists_exact_semantic_answer_and_replays(tmp_path,choice,value):
    store = store_for(tmp_path)
    questions,bundle = inputs()
    before_authority = copy.deepcopy(store.load(RUN_ID).get('authorizations'))
    async def prepare(**kwargs):
        return copy.deepcopy(bundle)
    session = SimpleNamespace(interaction_choice_preparer=prepare)
    shown=[]
    async def emit(event):
        if event['type']=='ask_user':
            shown.append(event)
            session._ask_user_request.future.set_result({'ref':choice})
    request = AskUserRequest('first','AskUserQuestion',questions,asyncio.get_running_loop().create_future())
    result = await wait_for_interaction(request,kind='ask_user',session=session,emit=emit,
        store=store,run_id=RUN_ID,stage_key='work')
    assert result=={'ref':value} and request.resolved_answers==result
    record=store.load(RUN_ID)
    row=record['collaboration']['questions'][request.collaboration_question_id]
    assert row['status']=='answered' and row['answer']==result and row['answer_source']=='user'
    assert record.get('authorizations')==before_authority
    assert 'work' not in record['collaboration']['certificates']
    replay=AskUserRequest('again','AskUserQuestion',copy.deepcopy(inputs()[0]),asyncio.get_running_loop().create_future())
    assert await wait_for_interaction(replay,kind='ask_user',session=session,emit=emit,
        store=store,run_id=RUN_ID,stage_key='work')==result
    assert len(shown)==1


@pytest.mark.parametrize('violation',['wrong_value','wrong_id','changed_bundle','wrong_source','changed_scope'])
def test_prepared_answer_validation_never_widens_frozen_question(tmp_path,violation):
    store=store_for(tmp_path)
    record=store.load(RUN_ID)
    questions,bundle=inputs()
    row=ask_gateway.prepare(record,'work',project_questions(questions,bundle),category='authorization')
    answer={'ref':DENY}
    selections={'ref':'ui-deny-28'}
    source='user'
    if violation=='wrong_value': answer={'ref':ALLOW}
    if violation=='wrong_id': selections={'ref':'unknown-id'}
    if violation=='changed_bundle': bundle['questions'][0]['choices'][1]['value']=ALLOW
    if violation=='wrong_source': source='auto_selected_recommended'
    if violation=='changed_scope': record['collaboration']['entries']['goal']['version']+=1
    before=copy.deepcopy(record)
    with pytest.raises(ValueError):
        ask_gateway.answer(record,row['id'],answer,source=source,choice_bundle=bundle,
            choice_answers=selections,choice_inputs={})
    assert record==before
