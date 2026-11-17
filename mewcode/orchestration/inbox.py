"""Read-only aggregation of host-owned decisions for one task.

Input schemas and source identities are retained. Display text is a separate
projection; it never edits canonical answers or supplies an approval default.
"""
from __future__ import annotations

import copy
import re
import uuid

from mewcode.kernel.events import digest
from mewcode.orchestration.ask_gateway import present

STALE = '该事项已经处理或已过期，请以当前显示的内容为准'
MISSING = '找不到这个待处理事项'
KINDS = frozenset({'permission', 'human_oncall', 'ask_user', 'plan_revision', 'file_grant',
                   'collaboration_recovery', 'run_inputs', 'recovery_case', 'team_intervention', 'team_plan',
                   'revision_draft', 'lesson', 'design_choice', 'maintenance_question', 'maintenance_proposal', 'maintenance_adoption', 'run_action'})


def design_choices(task, candidate, cached=None):
    """Only active business questions; original chooser owns all validation."""
    if not candidate or not candidate.get('autobuild'):
        return []
    from mewcode.orchestration.graph_discussion import issue_rows
    from mewcode.orchestration.interaction_choices import project_questions
    questions = [dict(name=row['id'], question=row.get('title', ''), message=row.get('body', ''),
                      options=copy.deepcopy(row.get('options') or []))
                 for row in issue_rows(candidate.get('document') or {}) if row.get('type') == 'business']
    bundle = (cached or {}).get('bundle') or {}
    projected = project_questions(questions, bundle) if bundle else questions
    result = []
    for question in projected:
        source_id = question['name']
        prepare = not bundle and not question.get('options')
        context = dict(candidate_id=candidate['id'], sequence=candidate['sequence'], question_id=source_id,
                       choice_bundle_id=bundle.get('id', ''), choice_version=bundle.get('version', ''))
        item = make('design_choice', task, source_id, title='确认设计中的取舍',
            background=question.get('message') or question.get('label'), questions=[] if prepare else [question],
            basis=[task.get('design_revision'), candidate['id'], candidate['sequence'], candidate.get('document'), bundle.get('version')], context=context)
        item.update(choice_bundle=copy.deepcopy(bundle), prepare_choices=prepare)
        result.append(item)
    return result


def permission_identity(runtime, payload, *, run_id='', node_id='', epoch=0):
    generation = getattr(runtime, 'permission_request_epoch', 0) + 1
    runtime.permission_request_epoch = generation
    return dict(payload, request_id=uuid.uuid4().hex, request_epoch=generation,
                conversation_id=getattr(runtime, 'conversation_id', ''), run_id=run_id,
                node_id=node_id, node_epoch=epoch)


def item_id(kind, task, source_id, basis=None):
    return kind + '.' + digest([task.get('conversation_id', ''), task['id'], task.get('run_id', ''), source_id, basis])


def known_id(value):
    kind, _, token = str(value).partition('.')
    return kind in KINDS and bool(re.fullmatch(r'[0-9a-f]{64}', token))


def safe_text(text, labels, fallback):
    value = present([{'question': str(text or '')}], internal_ids=list(labels), labels=labels)[0]['question']
    from mewcode.kernel.blockers import Blocker
    prohibited = r'\b(?:N|T)\d{2,}\b|`|\.mewcode[/\\]|\b(?:' + '|'.join(re.escape(row.value) for row in Blocker if row.value) + r')\b'
    return fallback if not value.strip() or re.search(prohibited, value, re.I) else value


def questions_for_display(questions, labels):
    result = []
    for index, raw in enumerate(questions or []):
        question = copy.deepcopy(raw) if isinstance(raw, dict) else dict(name='input_' + str(index), type='text', question=str(raw))
        question.setdefault('name', 'q' + str(index))
        for field in ('question', 'message', 'label', 'description', 'context', 'input_label', 'details'):
            if field in question:
                question[field] = safe_text(question[field], labels, '请补充这项信息' if field != 'context' else '请根据当前任务选择处理方式。')
        for field in ('options', 'choices', 'prepared_choices'):
            for number, option in enumerate(question.get(field) or []):
                if not isinstance(option, dict):
                    option = {'value': option, 'label': str(option)}
                    question[field][number] = option
                option['label'] = safe_text(option.get('label') or option.get('value') or option.get('id'), labels, '选项 ' + str(number + 1))
                for text_field in ('description', 'consequence', 'input_label'):
                    if text_field in option:
                        option[text_field] = safe_text(option[text_field], labels, '请查看该选项的任务依据。')
        result.append(question)
    return result


def choice(name, text, options, **extra):
    return dict(name=name, question=text, type='choice', options=[dict(value=value, label=label) for value, label in options], **extra)


def make(kind, task, source_id, *, title, background='', questions=(), basis=None, gate=None, labels=None, context=None, live=True):
    labels, gate = labels or {}, gate or {}
    mandatory = not (gate.get('reversible') is True and gate.get('kind') in {'plan_approval', 'business_choice', 'escalation_exhausted'}
                     and kind in {'ask_user', 'human_oncall'})
    if any(row.get('requires_user_answer') is True for row in questions if isinstance(row, dict)):
        mandatory = True
    return dict(id=item_id(kind, task, source_id, basis), type=kind, source_id=source_id,
                conversation_id=task.get('conversation_id', ''), task_id=task['id'], run_id=task.get('run_id', ''),
                title=safe_text(title, labels, '请确认下一步处理方式')[:40],
                background=safe_text(background, labels, '请根据当前任务作出明确决定。'),
                input_schema=copy.deepcopy(list(questions)), questions=questions_for_display(questions, labels),
                mandatory=mandatory, deadline=None if mandatory else gate.get('deadline'),
                timeout_consequence='持续等待你的明确选择。' if mandatory else gate.get('timeout_consequence', ''),
                recommended=copy.deepcopy(gate.get('recommended')), context=copy.deepcopy(context or {}), live=live)


def _request_payload(request):
    result = dict(request_id=request.request_id, questions=copy.deepcopy(request.questions),
                  tool=getattr(request, 'tool_name', ''), gate=copy.deepcopy(getattr(request, 'host_gate', {})),
                  scope_hash=getattr(request, 'gate_scope_hash', ''))
    bundle = getattr(request, 'choice_bundle', None)
    if bundle:
        result.update(choice_bundle=copy.deepcopy(bundle), source_questions=copy.deepcopy(getattr(request, 'source_questions', request.questions)))
    return result


def collect(task, *, record=None, runtime=None, design=None, lessons=(), startup=(), team_items=()):
    record = record or {}
    labels = {str(row.get('node_id') or row.get('id')): row.get('label') or '当前步骤' for row in (record.get('locked_flow') or {}).get('nodes', [])}
    result, seen = [], set()
    run_id = task.get('run_id') or ''
    matching_runtime = bool(runtime is not None and run_id and getattr(runtime, 'expert_run_id', '') == run_id)

    def prompt(kind, pending, *, live, node_id='', request=None):
        identity = pending.get('request_id')
        if not identity or identity in seen:
            return
        seen.add(identity)
        bundle = pending.get('choice_bundle') or {}
        gate = pending.get('gate') or (record.get('kernel_gates') or {}).get(identity) or {}
        context = dict(request_id=identity, node_id=node_id, choice_bundle_id=bundle.get('id', ''), choice_version=bundle.get('version', ''))
        questions = pending.get('questions') or []
        basis = [bundle.get('version'), gate.get('scope_identity'),
                 getattr(request, 'task_design_binding', None), pending.get('scope_hash') or '']
        row = make(kind, task, identity, title='请回答任务中的问题', background=(questions[0].get('context', '') if questions and isinstance(questions[0], dict) else ''),
                   questions=questions, basis=basis, gate=gate, labels=labels, context=context, live=live)
        row['choice_bundle'] = copy.deepcopy(bundle)
        row['input_schema'] = copy.deepcopy(pending.get('source_questions') or questions)
        if not live:
            row.update(resume_required=True, background='任务已暂停。继续任务后将重新核对并显示这个问题。')
        result.append(row)

    for kind in ('human_oncall', 'ask_user'):
        request = getattr(runtime, kind + '_request', None) if runtime is not None else None
        binding = getattr(request, 'task_design_binding', None) or {}
        planning = not run_id and binding.get('task_id') == task['id']
        if request is None or not (matching_runtime or planning):
            continue
        if request.future.done():
            seen.add(request.request_id)
            continue
        payload = _request_payload(request)
        prompt(kind, payload, live=True, request=request)
        for _, member in getattr(request, 'group_members', ()):
            seen.add(member.request_id)

    future = getattr(runtime, 'permission_future', None) if matching_runtime else None
    permission = getattr(runtime, 'permission_request', {}) or {} if matching_runtime else {}
    if future is not None and future.done() and permission.get('request_id'):
        seen.add(permission['request_id'])
    if future is not None and not future.done() and permission.get('request_id'):
        identity = permission['request_id']
        options = [('allow', '仅允许这一次'), ('deny', '不允许')]
        if permission.get('source') != 'review_missing_authorization' and permission.get('tool') not in {'Bash', 'RunVerification', 'TerminalStart', 'TerminalInput'}:
            options.insert(1, ('allow_always', '允许并记住这项权限'))
        if permission.get('directory_scope') and permission.get('directory_ttl_seconds', 0) > 0:
            options.insert(-1, ('allow_directory', '按所列范围授权目录'))
        result.append(make('permission', task, identity, title='确认这次操作权限',
            background=permission.get('description'), questions=[choice('decision', '如何处理这次权限请求', options)],
            basis=permission, labels=labels, context=permission))
        seen.add(identity)
    for bucket in ('node_states', 'members', 'stages'):
        for node_id, stage in (record.get(bucket) or {}).items():
            pending = stage.get('pending_interaction') or {}
            if pending.get('kind') in {'ask_user', 'human_oncall'}:
                prompt(pending['kind'], pending, live=False, node_id=node_id)
            pending = stage.get('pending_permission') or {}
            identity = pending.get('request_id') or pending.get('tool_id')
            if identity and identity not in seen:
                seen.add(identity)
                result.append(make('permission', task, identity, title='继续后确认操作权限',
                    background='权限请求尚未作答。继续任务后会生成当前操作的权限卡。', basis=[node_id, pending],
                    context=dict(node_id=node_id, request_id=identity), live=False))
                result[-1]['resume_required'] = True
    proposal = record.get('pending_recovery_revision')
    if proposal:
        result.append(make('plan_revision', task, digest(proposal), title='确认计划修订', background=proposal.get('summary'),
            questions=[choice('action', '如何处理这项修订', [('approve', '批准这项修订'), ('supply_info', '补充信息后继续'), ('reject', '保留原计划')])],
            basis=proposal, labels=labels, context=dict(proposal=proposal)))
    if matching_runtime:
        for offer in getattr(runtime, 'pending_file_grants', ()):
            result.append(make('file_grant', task, digest(offer), title='确认文件访问范围', background='文件：' + str(offer.get('path', '')),
                questions=[choice('action', '是否按所列范围授权', [('grant', '按所列范围授权'), ('decline', '暂不授权')])],
                basis=offer, context=offer, labels=labels))
    if record.get('collaboration'):
        from mewcode.orchestration.task_view import collaboration_projection
        for gate in collaboration_projection(record)['recovery_gates']:
            options = [('retry', '允许再试一次'), ('switch_model', '换模型后重试'), ('revise', '修改要求后再继续')]
            if gate.get('verifier_switch_available'):
                options.insert(1, ('switch_verifier', '更换当前步骤的验收模型'))
            if gate.get('kind') == 'coordinator_unavailable':
                options = [('resume_coordinator', '恢复当前协调模型'), ('switch_coordinator', '选择其他协调模型'), ('revise', '修改要求后再继续')]
            if gate.get('kind') == 'lineage_budget_exhausted':
                options = [('add_budget', '追加指定额度后继续'), ('revise', '修改要求后再继续'), ('inspect', '查看当前工作')]
            from mewcode.kernel.collaboration_recovery import elastic_budget_available
            if elastic_budget_available(record['collaboration'], gate):
                options.insert(0, ('enable_elastic_budget', '改为弹性额度，共享任务总额'))
            if gate.get('kind') == 'execution_evidence_available':
                options = [('recheck_execution_evidence', '复用原交付重新验收'), ('revise', '修改要求后再继续')]
            if gate.get('manual_available') and gate.get('kind') not in {'coordinator_unavailable', 'execution_evidence_available'}:
                options.append(('accept', '根据现有证据人工验收通过'))
            if gate.get('effects_unresolved'):
                options = [('revise', '修改要求后再继续'), ('inspect', '先核对操作结果')]
            result.append(make('collaboration_recovery', task, gate['id'], title='选择恢复方式', background=gate['reason'],
                questions=[choice('action', '如何继续这项工作', options), dict(name='reason', type='text', question='说明这次决定的依据', required=True)],
                basis=gate['basis'], labels=labels, context=gate))
    from mewcode.experts.recovery_case import cases, decision_version
    # cases()/NodeLedger.view() return detached facts; only the top-level
    # default ledger mapping needs isolation, not unrelated execution history.
    case_record = dict(record)
    for case in cases(case_record) if record else []:
        pending = case.get('pending') or {}
        if (case.get('status') not in {'reported', 'awaiting_user'} or not pending
                or (case.get('handling') or {}).get('owner') == 'system'):
            continue
        from mewcode.experts.node_ledger import ledger_for_run
        stage = (record.get('node_states') or {}).get(case['node_id'], {})
        if (stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}
                or ledger_for_run(case_record).view(case['node_id']).epoch != case['epoch']
                or stage.get('input_revision', '') != case.get('input_revision', '')):
            continue
        source = copy.deepcopy(pending.get('source_questions') or pending.get('input_definitions') or pending.get('questions') or [])
        bundle = pending.get('choice_bundle') or pending.get('prepared_choice_bundle') or pending.get('bundle') or {}
        questions = copy.deepcopy(source)
        if bundle:
            from mewcode.orchestration.interaction_choices import project_questions
            questions = project_questions(source, bundle)
        options = copy.deepcopy(pending.get('options') or pending.get('recovery_options') or [])
        if options and not bundle:
            questions.append(dict(name='choice', type='choice', question='如何处理当前问题', options=options,
                                  preserve_choice_value=True))
        result.append(make('recovery_case', task, case['case_id'], title='确认问题的处理方式',
            background=(case.get('handling') or {}).get('reason') or (case.get('report') or {}).get('summary') or
                       (case.get('report') or {}).get('reason') or pending.get('reason'), questions=questions,
            basis=[decision_version(case), pending], context=case, labels=labels))
        result[-1].update(choice_bundle=copy.deepcopy(bundle), input_schema=source or copy.deepcopy(questions))
    for cached in startup:
        bundle = cached.get('bundle') or {}
        if cached.get('answered_version') == bundle.get('version'):
            continue
        from mewcode.orchestration.interaction_choices import project_questions
        result.append(make('run_inputs', task, bundle.get('id'), title='确认本次运行参数',
            questions=project_questions(cached.get('questions') or [], bundle), basis=bundle,
            context=dict(choice_bundle_id=bundle.get('id'), choice_version=bundle.get('version')), labels=labels))
        result[-1].update(choice_bundle=copy.deepcopy(bundle), input_schema=copy.deepcopy(cached.get('questions') or []))
    for row in lessons:
        if row.get('status') == 'pending' and row.get('run_id') == run_id:
            result.append(make('lesson', task, row['lesson_id'], title='是否采用这条任务经验', background=row.get('text'),
                questions=[choice('action', '如何处理这条经验', [('accept', '采用'), ('decline', '不采用')])],
                basis=[row.get('text'), row.get('status')], context=dict(flow_id=row['flow_id'], lesson_id=row['lesson_id']), labels=labels))
    for row in team_items:
        kind = row['type']
        questions = [choice('action', '如何处理这项变更', [('accept', '确认这项变更'), ('decline', '不采用')])]
        if kind == 'team_intervention' and row.get('source', {}).get('state') == 'needs_clarification':
            questions = [dict(name='request', type='team_intervention', question='明确这条消息的意图与修改范围'),
                         dict(name='reason', type='text', question='说明你要怎样修改', required=True)]
        result.append(make(kind, task, row['source_id'], title=row['title'], background=row.get('background'),
            questions=questions, basis=row.get('basis'), context=dict(source=row.get('source'), basis=row.get('basis')), labels=labels))
    for row in (record.get('workbench_revision_drafts') or {}).values():
        if row.get('status') == 'pending':
            result.append(make('revision_draft', task, row['id'], title='确认对话中的要求修改', background=row.get('summary'),
                questions=[choice('action', '是否采用这些要求修改', [('accept', '确认这些修改'), ('decline', '不采用')])],
                basis=[row.get('digest'), row.get('base_version')], context=row, labels=labels))
    return result
