"""One validation path for desktop, workbench and remote prompt answers.

Remote wire compatibility is an adapter, never a weaker approval validator.
No missing answer, recommended choice or timeout authorizes an operation.
"""
from __future__ import annotations

import copy

from mewcode.agent import PermissionResponse
from mewcode.kernel.events import digest

STALE = '该事项已经处理或已过期，请以当前显示的内容为准'
MISSING = '找不到这个待处理事项'


class PromptError(ValueError):
    def __init__(self, message=STALE, status=409, *, code='stale_request'):
        super().__init__(message)
        self.status, self.code = status, code


def revision_reply_id(conversation_id, run_id, proposal):
    return 'revision:' + digest([conversation_id, run_id, proposal])


def remote_request_id(request):
    bundle = getattr(request, 'choice_bundle', None)
    if not bundle:
        return request.request_id
    return str(request.request_id) + '.wb.' + digest([bundle, getattr(request, 'choice_display_signature', None),
        getattr(request, 'task_design_binding', None), getattr(request, 'choice_run_id', '')])


def remote_prompt(runtime, prompt):
    value = copy.deepcopy(prompt)
    kind = value.get('type')
    if kind in {'ask_user', 'human_oncall'} and runtime is not None:
        request = getattr(runtime, kind + '_request', None)
        if request is not None and request.request_id == value.get('request_id'):
            value['request_id'] = remote_request_id(request)
    return value


def adapt_remote(runtime, kind, message):
    value = dict(message)
    if kind in {'ask_user', 'human_oncall'} and runtime is not None:
        request = getattr(runtime, kind + '_request', None)
        if request is not None and getattr(request, 'choice_bundle', None):
            if value.get('request_id') != remote_request_id(request):
                raise PromptError()
            value.update(request_id=request.request_id, choice_bundle_id=request.choice_bundle['id'],
                         choice_version=request.choice_bundle['version'])
    return value


def validate_request_basis(request, message):
    from mewcode.orchestration.task_design import validate_question_binding
    try:
        if getattr(request, 'task_design_binding', None) is not None:
            validate_question_binding(*request.task_design_owner, request.task_design_binding)
        bundle = getattr(request, 'choice_bundle', None)
        if bundle:
            if message.get('choice_bundle_id') != bundle.get('id') or message.get('choice_version') != bundle.get('version'):
                raise ValueError(STALE)
            signature = getattr(request, 'choice_state_signature', None)
            if callable(signature) and signature() != getattr(request, 'choice_display_signature', None):
                raise ValueError(STALE)
    except ValueError:
        raise PromptError() from None


async def dispatch(srv, kind, message, *, transport='desktop', runtime=None):
    kind = 'ask_user' if kind == 'askuser' else kind
    message = dict(message)
    cid = str(message.get('conversation_id') or '')
    runtime = runtime if runtime is not None else srv.SESSION.runtime_for(cid)
    if runtime is not None and getattr(runtime, 'conversation_id', cid) != cid:
        raise PromptError()
    if transport == 'remote':
        message = adapt_remote(runtime, kind, message)
    if kind == 'permission':
        future = getattr(runtime, 'permission_future', None)
        pending = getattr(runtime, 'permission_request', {}) or {}
        if future is None or future.done():
            raise PromptError()
        if getattr(runtime, 'permission_request_future', future) is not future:
            raise PromptError()
        decision = message.get('decision')
        # Older remote clients could send an unrecognized rejection. Preserve
        # its denial semantics only; no legacy compatibility may grant access.
        legacy_deny = transport == 'remote' and not pending.get('request_id') and decision not in {'allow', 'allow_always', 'allow_directory'}
        if not legacy_deny and (not pending.get('request_id') or message.get('request_id') != pending['request_id']):
            raise PromptError()
        if pending.get('run_id') and (pending['run_id'] != getattr(runtime, 'expert_run_id', '')
                or message.get('run_id') not in {None, '', pending['run_id']}):
            raise PromptError()
        if pending.get('run_id'):
            record = srv.EXPERT_RUNS.load(pending['run_id']) or {}
            stage = next((rows.get(pending.get('node_id')) for rows in [record.get('node_states', {}), record.get('members', {})]
                          if rows.get(pending.get('node_id')) is not None), {})
            current = stage.get('pending_permission') or {}
            if (record.get('conversation_id') != cid or current.get('request_id') != pending['request_id']
                    or stage.get('epoch', 0) != pending.get('node_epoch', 0)):
                raise PromptError()
        choices = {'allow': PermissionResponse.ALLOW, 'allow_always': PermissionResponse.ALLOW_ALWAYS,
                   'allow_directory': PermissionResponse.ALLOW_DIRECTORY, 'deny': PermissionResponse.DENY}
        if decision not in choices and not legacy_deny:
            raise PromptError('请选择明确的权限决定', 400, code='invalid_answer')
        future.set_result(choices.get(decision, PermissionResponse.DENY))
        from mewcode.orchestration.workbench_events import changed
        changed('runtime', conversation_id=cid, run_id=pending.get('run_id', ''))
        return dict(ok=True, kind=kind, decision=decision if decision in choices else 'deny')
    if kind == 'human_oncall':
        request = getattr(runtime, 'human_oncall_request', None)
        if request is not None:
            validate_request_basis(request, message)
        result = srv._human_oncall_response_result(runtime, message)
        if not result['ok']:
            if result.get('error_type') in {'no_live_request', 'stale_request'}:
                raise PromptError()
            raise PromptError(result['error'], 400, code=result.get('error_type', 'invalid_answer'))
        from mewcode.orchestration.workbench_events import changed
        changed('runtime', conversation_id=cid)
        return result
    if kind == 'ask_user':
        request = getattr(runtime, 'ask_user_request', None)
        if request is None or request.future.done() or message.get('request_id') != request.request_id:
            raise PromptError()
        validate_request_basis(request, message)
        cancelled = bool(message.get('cancelled'))
        answers = {} if cancelled else (message.get('answers') or {})
        try:
            note_answers = {name: (', '.join(value['custom']) if isinstance(value.get('custom'), list) else value['custom'])
                            if isinstance(value, dict) and set(value) == {'custom'} else value for name, value in answers.items()}
            notes = {} if cancelled else srv.validate_answer_notes(request.questions, note_answers, message.get('notes'))
            answers = srv._resolve_prepared_response(request, message, answers)
        except (ValueError, AttributeError) as exc:
            raise PromptError(str(exc), 400, code='invalid_answer') from None
        request.answer_notes = notes
        request.future.set_result(answers)
        from mewcode.orchestration.workbench_events import changed
        changed('runtime', conversation_id=cid)
        return dict(ok=True, kind=kind, cancelled=cancelled)
    if kind != 'plan_revision':
        raise PromptError('unsupported prompt kind', 400, code='unknown_kind')
    run_id = str(message.get('run_id') or '')
    # The remote route attests ownership by activating the requested chat
    # before dispatch. Desktop callers must already be on that chat.
    if not cid or (transport != 'remote' and cid != srv._active_conversation_id()):
        raise PromptError('请先切换到该专家任务所属对话，再处理计划修订。')
    snapshot = srv.EXPERT_RUNS.load(run_id) if run_id else None
    proposal = (snapshot or {}).get('pending_recovery_revision')
    if not snapshot or snapshot.get('conversation_id') != cid or not proposal:
        raise PromptError()
    action = str(message.get('recovery_action') or message.get('action') or message.get('decision') or '').strip()
    reply_id = revision_reply_id(cid, run_id, proposal)
    frozen = message.get('proposal')
    explicit_basis = message.get('request_id') == reply_id or (isinstance(frozen, dict) and digest(frozen) == digest(proposal))
    legacy_reject = transport == 'remote' and not message.get('request_id') and action in {'reject', 'defer'}
    if not explicit_basis and not legacy_reject:
        raise PromptError()
    if action in {'reject', 'defer'}:
        if legacy_reject:
            srv.EXPERT_RUNS.reject_recovery_revision(run_id)
        else:
            srv.EXPERT_RUNS.reject_recovery_revision(run_id, expected=proposal)
        return dict(ok=True, kind=kind, status='rejected', run_id=run_id)
    if action not in {'approve', 'supply_info'}:
        raise PromptError('请选择明确的计划修订决定', 400, code='invalid_answer')
    if action == 'supply_info' and not str(message.get('recovery_info') or message.get('info') or '').strip():
        raise PromptError('补充信息不能为空', 400, code='invalid_answer')
    if srv.SESSION.chat_busy(cid):
        raise PromptError('当前对话仍有任务运行')
    started = await srv.launch_expert_resume(snapshot, recovery_action=action,
        recovery_info=str(message.get('recovery_info') or message.get('info') or ''),
        grant_extra_budget=message.get('grant_extra_budget') is True, sync_canvas=message.get('sync_canvas') is True)
    return dict(ok=True, kind=kind, **started)
