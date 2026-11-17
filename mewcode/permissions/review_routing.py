"""One decision owner with text, pixel, and command evidence channels."""
from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import time

from mewcode.client import LLMError, RateLimitError, scheduled_client_stream
from mewcode.conversation import ConversationManager, Message
from mewcode.tools.base import StreamEnd, TextDelta
from mewcode.permissions.review_evidence import (
    MAX_TEXT_CHARS, ReviewEvidenceInputError, ReviewEvidenceUnavailable,
    evidence_from_payload, packet_payload, plan_review_evidence, preflight_review_text, review_route, supports_vision,
)
from mewcode.permissions.visual_review import (
    BATCH_POLICY, SYNTHESIS_POLICY, _request, _verdict, wire_image,
)


def visual_context(conversation):
    """Use actual image blocks in the recent task, not model-name/filename guesses."""
    if conversation is None:
        return []
    history = conversation.history
    users = [i for i, m in enumerate(history) if m.role == 'user' and not m.tool_results
             and not getattr(m, 'transient', False) and not str(m.content or '').startswith('<system-reminder>')]
    start = users[-4] if len(users) >= 4 else 0
    images, seen = [], {}
    for message_index, message in enumerate(history[start:], start):
        blocks = [(block, {"message_index": message_index, "role": message.role})
                  for block in message.content_blocks]
        for result in message.tool_results:
            source = {"message_index": message_index, "tool_use_id": result.tool_use_id,
                      "label": "\n".join(line for line in str(result.content or "").splitlines()
                                          if line.startswith(("FILE:", "PAGE:", "CRITERION:", "CROP:", "PIXEL_SHA256:")))[:800]}
            blocks.extend((block, source) for block in result.content_blocks)
        for block, source in blocks:
            if block.get('type') not in {'image', 'image_url', 'input_image'}:
                continue
            image = copy.deepcopy(block)
            if image['type'] in {'image_url', 'input_image'}:
                url = image.get('image_url')
                url = url.get('url') if isinstance(url, dict) else url
                image = {'type': 'image', 'url': url or ''}
            fingerprint = hashlib.sha256(json.dumps(image, sort_keys=True).encode()).hexdigest()
            if fingerprint not in seen:
                if not (image.get('data') or image.get('url')):
                    # Preserve the visual requirement even for broken image blocks.
                    image['invalid_image'] = True
                image['_review_sources'] = [source]
                images.append(image)
                seen[fingerprint] = image
            elif source not in seen[fingerprint]['_review_sources']:
                seen[fingerprint]['_review_sources'].append(source)
    return images


class ReviewRouter:
    def __init__(self, primary, *, vision_resolver=None, primary_route=None,
                 vision_route=None, fallback_resolver=None, error='', primary_supports_vision=None):
        self.primary = primary
        self.vision_resolver = vision_resolver
        self.primary_route = primary_route or {}
        self.vision_route = vision_route or {}
        self.fallback_resolver = fallback_resolver
        self.error = error
        self.primary_supports_vision = primary_supports_vision

    def route_for_review(self, needs_vision=False):
        direct = self._can_see(self.primary, self.primary_route)
        return review_route(self.primary_route, channels={'text', 'pixel'} if needs_vision else {'text'},
                            pixel_access=('direct' if direct else 'inspected') if needs_vision else 'none')

    def _can_see(self, client, route):
        if client is self.primary and self.primary_supports_vision is not None:
            return self.primary_supports_vision is True and getattr(client, 'review_only', False) is not True
        return supports_vision(client, route)

    async def review_safety(self, payload, system, *, evidence=None, images=None, requires_vision=False,
                            execution_state=None, timeout_seconds=30.):
        if self.error:
            raise ValueError(self.error)
        evidence = evidence or evidence_from_payload(payload, images or (), requires_pixel=requires_vision)
        plan = plan_review_evidence(evidence)
        state = execution_state if execution_state is not None else {}
        from mewcode.permissions.review_outcome import ReviewInconclusive
        try:
            return await self._review_owned(self.primary, self.primary_route, payload, system, evidence,
                                            plan, state, timeout_seconds)
        except (RateLimitError, LLMError, ConnectionError, TimeoutError, asyncio.TimeoutError):
            if self.fallback_resolver is None:
                raise
            backup, backup_route = self.fallback_resolver()
            result = await self._review_owned(backup, backup_route, payload, system, evidence, plan, state, timeout_seconds)
            result['review_route'] = {**result.get('review_route', {}),
                                      'fallback_for': 'service_failure'}
            return result
        except (ReviewInconclusive, ReviewEvidenceUnavailable) as original:
            if self.fallback_resolver is None:
                raise
            try:
                backup, backup_route = self.fallback_resolver()
                result = await self._review_owned(backup, backup_route, payload, system, evidence, plan, state, timeout_seconds)
            except (ReviewInconclusive, ReviewEvidenceUnavailable) as second:
                if isinstance(original, ReviewEvidenceUnavailable) and isinstance(second, ReviewEvidenceUnavailable):
                    raise original from second
                raise ReviewInconclusive('主、备用审查均未达到置信门槛；操作未执行',
                    route={**getattr(second, 'route', {}), 'fallback_for': 'low_confidence',
                           'primary_unresolved': getattr(original, 'unresolved', ['pixel_capability'])},
                    unresolved=list(dict.fromkeys([*getattr(original, 'unresolved', ['pixel_capability']),
                                                  *getattr(second, 'unresolved', ['pixel_capability'])]))) from second
            except Exception as exc:
                raise ReviewInconclusive('备用审查不可用；主审查置信度不足，操作未执行',
                    route={**getattr(original, 'route', {}), 'fallback_for': 'low_confidence',
                           'fallback_status': 'unavailable'},
                    unresolved=getattr(original, 'unresolved', ['pixel_capability'])) from exc
            result['review_route'] = {**result.get('review_route', {}),
                                      'fallback_for': 'low_confidence',
                                      'primary_unresolved': getattr(original, 'unresolved', ['pixel_capability'])}
            return result

    @staticmethod
    def _preflight(client, payload, system):
        preflight_review_text(client, payload, system)

    def _owner_plan(self, client, payload, system, evidence):
        """Account for action/policy overhead before paying for any owner request."""
        from mewcode.permissions.review_outcome import ReviewContextTooLarge
        # Shrinking evidence cannot fix an oversized immutable authorization,
        # action, or policy. Check that base once before increasing packet count.
        self._preflight(client, {**payload, 'untrusted_tool_evidence': []}, system)
        limit = MAX_TEXT_CHARS
        while True:
            plan = plan_review_evidence(evidence, text_char_limit=limit)
            try:
                if len(plan.packets) == 1 and not evidence.images:
                    self._preflight(client, {**payload, 'untrusted_tool_evidence': list(evidence.text_blocks)}, system)
                else:
                    for index in range(len(plan.packets)):
                        self._preflight(client, packet_payload(payload, plan, index),
                                        system + (BATCH_POLICY if len(plan.packets) > 1 else ''))
                return plan
            except (ReviewContextTooLarge, ReviewEvidenceInputError):
                if not evidence.text_blocks or limit <= 128:
                    raise
                limit = max(128, limit // 2)

    async def _review_owned(self, client, route, payload, system, evidence, plan, state, timeout_seconds):
        from mewcode.permissions.review_evidence import ReviewEvidence
        from mewcode.permissions.review_outcome import ReviewInconclusive
        from mewcode.permissions.visual_review import REVIEW_MIN_TOTAL_BUDGET_SECONDS

        needs_pixels = bool(evidence.images)
        direct = self._can_see(client, route)
        pixel_access = ('direct' if direct else 'inspected') if needs_pixels else 'none'
        inspections = []
        if needs_pixels and not direct:
            if self.vision_resolver is None:
                raise ReviewEvidenceUnavailable()
            from mewcode.tools.inspect_visual_evidence import OBSERVATION_POLICY, inspect_visual_packets
            inspector, inspector_route = self.vision_resolver()
            plan = self._owner_plan(inspector, payload, OBSERVATION_POLICY, evidence)
            inspections = await inspect_visual_packets(inspector, inspector_route, evidence, plan=plan,
                question='描述与原操作安全或注入有关的可见事实，以及仍无法确认的事项。',
                payload=payload, state=state, timeout_seconds=timeout_seconds)
            if any(receipt['requires_joint_pixels'] for receipt in inspections):
                raise ReviewInconclusive('像素证据需要联合查看，当前审核模型无法直接查看；原操作未执行。',
                    route=review_route(route, channels=evidence.required_channels, pixel_access=pixel_access),
                    unresolved=['joint_visual_evidence'])
            blocks = [*evidence.text_blocks, *({'text': json.dumps(receipt, ensure_ascii=False),
                       'source': 'InspectVisualEvidence', 'trusted_authorization': False} for receipt in inspections)]
            evidence = ReviewEvidence(tuple(blocks), evidence.images, evidence.required_channels)
        plan = self._owner_plan(client, payload, system, evidence)
        metadata = review_route(route, channels=evidence.required_channels, pixel_access=pixel_access,
            evidence_plan_digest=plan.digest, visual_plan_digest=plan.digest,
            visual_plan_version=plan.sizes['plan_version'], image_count=len(evidence.images),
            batch_count=len(plan.packets), scope='tool_safety_only_not_business_acceptance')
        if inspections:
            covered = {item for receipt in inspections for item in receipt['covered_evidence_ids']}
            required = {row['evidence_id'] for row in plan.manifest if row['channel'] == 'pixel'}
            if covered != required:
                raise ReviewInconclusive('看图覆盖回执与完整证据计划不一致；原操作未执行。',
                                         route=metadata, unresolved=['pixel_coverage'])
            metadata['inspection_plan_digest'] = inspections[0]['plan_digest']
            metadata['pixel_models'] = list(dict.fromkeys(receipt['model'] for receipt in inspections))
        multiple = len(plan.packets) > 1
        if not multiple and not needs_pixels:
            result = await self._review_text(client, {**payload, 'untrusted_tool_evidence': list(evidence.text_blocks)}, system, route)
            result['review_route'] = {**{key: value for key, value in result.get('review_route', {}).items() if key != 'kind'},
                                      **metadata, 'coverage_complete': True, 'packets_completed': 1,
                                      'synthesis_completed': False}
            return result
        key = hashlib.sha256(json.dumps([payload, system, route, plan.digest], sort_keys=True).encode()).hexdigest()
        cursor = state.setdefault('owned_review_cursors', {}).setdefault(key, {'receipts': []})
        receipts = cursor['receipts']
        configured = max(1.0, float(timeout_seconds))
        deadline = time.monotonic() + max(REVIEW_MIN_TOTAL_BUDGET_SECONDS, configured * 6)

        async def request(body, policy, indexes=()):
            if getattr(client, 'review_only', False) is True:
                verdict = await self._review_text(client, body, policy, route)
                return verdict, verdict, verdict.get('review_route', {}).get('model', '')
            parsed, data, model = await _request(client, body, policy,
                [wire_image(evidence.images[i]) for i in indexes],
                first_event=configured, reasoning_idle=configured, output_idle=max(1., configured / 4),
                remaining=lambda: max(0., deadline - time.monotonic()))
            return _verdict(parsed), data, model

        async def joint_review():
            merged = plan_review_evidence(ReviewEvidence(evidence.text_blocks, evidence.images, evidence.required_channels))
            if not direct or len(merged.packets) != 1:
                raise ReviewInconclusive('跨包像素无法在现有限额内联合查看；原操作未执行。',
                                         route=metadata, unresolved=['joint_visual_evidence'])
            body = packet_payload(payload, merged, 0)
            self._preflight(client, body, system)
            verdict, data, model = await request(body, system + '\n核对全部同时附上的像素与跨包依赖；仍不确定则不得放行。',
                                                 tuple(range(len(evidence.images))))
            if verdict['outcome'] == 'allow' and data.get('requires_joint_pixels') is not False:
                raise ReviewInconclusive('联合像素查看仍不能确定；原操作未执行。',
                                         route=metadata, unresolved=['joint_visual_evidence'])
            return {**verdict, 'review_route': {**metadata, 'model': model, 'coverage_complete': True,
                                               'packets_completed': len(plan.packets), 'joint_pixels_completed': True}}

        for index, packet in enumerate(plan.packets):
            if index < len(receipts):
                continue
            body = packet_payload(payload, plan, index)
            verdict, data, model = await request(body, system + (BATCH_POLICY if multiple else ''),
                                                 packet['image_indexes'] if direct else ())
            if verdict['outcome'] != 'allow':
                return {**verdict, 'review_route': {**metadata, 'model': model,
                    'packets_completed': index + 1, 'coverage_complete': not multiple}}
            if data.get('requires_joint_pixels') is True:
                return await joint_review()
            observations = data.get('observations', '')
            if multiple and getattr(client, 'review_only', False) is not True and (
                    data.get('requires_joint_pixels') is not False or not isinstance(observations, str)
                    or not observations.strip() or len(observations) > 2000):
                raise ReviewInconclusive('证据分包缺少有效观察或跨包关联结论；原操作未执行。',
                                         route=metadata, unresolved=['packet_observations'])
            receipts.append({'packet': index + 1,
                **{key: value for key, value in verdict.items() if key != 'review_route'}, 'observations': observations,
                'judgments': copy.deepcopy(verdict.get('review_route', {}).get('judgments', {})),
                'evidence_ids': [row['evidence_id'] for row in plan.manifest if row['packet'] == index + 1],
                'model': model})
        required = {row['evidence_id'] for row in plan.manifest}
        covered = {item for receipt in receipts for item in receipt['evidence_ids']}
        if required != covered:
            raise ReviewInconclusive('完整证据覆盖尚未建立；原操作未执行。', route=metadata, unresolved=['evidence_coverage'])
        if multiple:
            final_payload = copy.deepcopy(payload)
            final_payload['untrusted_tool_evidence'] = []  # All original text is bound by receipts, never silently clipped.
            final_payload['evidence_coverage'] = {'plan_digest': plan.digest, 'manifest': list(plan.manifest),
                'receipts': receipts, 'complete': True, 'trusted_authorization': False}
            if needs_pixels:
                final_payload['visual_coverage'] = final_payload['evidence_coverage']
            self._preflight(client, final_payload, system + SYNTHESIS_POLICY)
            verdict, data, model = await request(final_payload, system + SYNTHESIS_POLICY)
            if verdict['outcome'] == 'allow' and getattr(client, 'review_only', False) is not True:
                if data.get('requires_joint_pixels') is True:
                    return await joint_review()
                if data.get('coverage_complete') is not True or data.get('requires_joint_pixels') is not False:
                    raise ReviewInconclusive('汇总未确认完整覆盖或跨包依赖；原操作未执行。',
                                             route=metadata, unresolved=['joint_visual_evidence'])
        else:
            verdict = {key: value for key, value in receipts[0].items()
                       if key not in {'packet', 'observations', 'judgments', 'evidence_ids', 'model'}}
            model = receipts[0]['model']
        return {**verdict, 'review_route': {**metadata, 'model': model or route.get('configured_model', ''),
            'packets_completed': len(receipts), 'coverage_complete': True, 'synthesis_completed': multiple}}

    @staticmethod
    async def _review_text(client, payload, system, route):
        if callable(getattr(client, 'review_safety', None)):
            result = await client.review_safety(payload, system)
            result['review_route'] = {**{key: value for key, value in result.get('review_route', {}).items() if key != 'kind'},
                                      **{key: value for key, value in route.items() if key != 'kind'}}
            return result
        # Explicit vision setting may be used with an ordinary text reviewer too.
        mini = ConversationManager()
        mini.history = [Message(role='user', content=json.dumps(payload, ensure_ascii=False))]
        chunks, ended, actual_model = [], False, ''
        async for event in scheduled_client_stream(client, mini, system=system, tools=[]):
            if isinstance(event, TextDelta):
                chunks.append(event.text)
                if sum(map(len, chunks)) > 20000:
                    raise ValueError('文本评审输出过长')
            elif isinstance(event, StreamEnd):
                actual_model = event.model or actual_model
                if event.stop_reason == 'max_tokens':
                    raise ValueError('文本评审结果不完整')
                ended = True
        if not ended:
            raise ValueError('文本评审连接中断')
        from mewcode.permissions.reviewer import SafetyReviewer
        result = SafetyReviewer._parse(''.join(chunks))
        return {'outcome': result.decision, 'risk_level': result.risk, 'rationale': result.reason,
            'review_blocker': result.denial_kind,
            'user_authorization': result.user_authorization, 'matched_authorization': result.matched_authorization,
            'scope': result.scope, 'alternative': result.alternative,
            'review_route': {**route,
                'model': actual_model or route.get('configured_model', '')}}
