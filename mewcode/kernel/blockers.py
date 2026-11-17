"""Closed blocker vocabulary and its host-owned resolution contract."""
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Blocker(StrEnum):
    NONE = ''
    PERMISSION = 'permission'
    UNKNOWN_EFFECT = 'unknown_effect'
    SAFETY_REVIEW = 'safety_review'
    REVIEW_FORMAT = 'safety_review_format'
    REVIEW_UNAVAILABLE = 'safety_review_unavailable'
    PLAN_REVISION = 'plan_revision_required'
    USER_INPUT = 'user_input'
    EXTERNAL_DEPENDENCY = 'external_dependency'
    RUN_DEADLINE = 'run_deadline'
    CAPABILITY_MISSING = 'capability_missing'
    PROVIDER_ERROR = 'provider_error'
    ENVIRONMENT_FAULT = 'environment_fault'
    NOT_STARTED = 'not_started'
    READONLY_UNKNOWN = 'readonly_effect_unknown'
    BUSINESS_AMBIGUITY = 'business_ambiguity'
    DELIVERY_FORMAT = 'delivery_format'
    INTERNAL_WORK = 'internal_work'
    MISSING_AUTHORIZATION = 'missing_authorization'
    INSUFFICIENT_EVIDENCE = 'insufficient_evidence'
    POLICY_VIOLATION = 'policy_violation'
    SEMANTIC_RISK = 'semantic_risk'
    CONTRACT_CONFLICT = 'contract_conflict'


@dataclass(frozen=True)
class BlockerPolicy:
    owner: str
    category: str
    clearance: str


REGISTRY = MappingProxyType({
    Blocker.PERMISSION: BlockerPolicy('human', 'authority', 'explicit_permission_receipt'),
    Blocker.UNKNOWN_EFFECT: BlockerPolicy('verifier', 'authority', 'independent_effect_receipt'),
    Blocker.SAFETY_REVIEW: BlockerPolicy('reviewer', 'authority', 'current_safety_review_receipt'),
    Blocker.REVIEW_FORMAT: BlockerPolicy('reviewer', 'auto', 'current_safety_review_receipt'),
    Blocker.REVIEW_UNAVAILABLE: BlockerPolicy('reviewer', 'auto', 'current_safety_review_receipt'),
    Blocker.PLAN_REVISION: BlockerPolicy('planner', 'business', 'approved_plan_revision'),
    Blocker.USER_INPUT: BlockerPolicy('human', 'business', 'answer_to_original_request_id'),
    Blocker.EXTERNAL_DEPENDENCY: BlockerPolicy('verifier', 'business', 'verified_dependency_receipt'),
    Blocker.RUN_DEADLINE: BlockerPolicy('human', 'authority', 'new_authorized_deadline'),
    Blocker.CAPABILITY_MISSING: BlockerPolicy('executor', 'auto', 'capability_binding_receipt'),
    Blocker.PROVIDER_ERROR: BlockerPolicy('executor', 'auto', 'completed_model_response'),
    Blocker.ENVIRONMENT_FAULT: BlockerPolicy('executor', 'auto', 'environment_ready_receipt'),
    Blocker.NOT_STARTED: BlockerPolicy('executor', 'auto', 'capability_binding_receipt'),
    Blocker.READONLY_UNKNOWN: BlockerPolicy('verifier', 'auto', 'readonly_retry_receipt'),
    Blocker.BUSINESS_AMBIGUITY: BlockerPolicy('human', 'business', 'explicit_contract_decision'),
    Blocker.DELIVERY_FORMAT: BlockerPolicy('executor', 'auto', 'capability_binding_receipt'),
    Blocker.INTERNAL_WORK: BlockerPolicy('executor', 'auto', 'completed_model_response'),
    Blocker.MISSING_AUTHORIZATION: BlockerPolicy('human', 'authority', 'explicit_permission_receipt'),
    Blocker.INSUFFICIENT_EVIDENCE: BlockerPolicy('verifier', 'auto', 'verified_dependency_receipt'),
    Blocker.POLICY_VIOLATION: BlockerPolicy('human', 'authority', 'explicit_contract_decision'),
    Blocker.SEMANTIC_RISK: BlockerPolicy('arbiter', 'business', 'explicit_contract_decision'),
    Blocker.CONTRACT_CONFLICT: BlockerPolicy('coordinator', 'business', 'approved_contract_revision'),
})

# Only legacy wire/storage spellings live here; new facts use canonical values.
ALIASES = MappingProxyType({
    'permission_denied': Blocker.PERMISSION, 'permission_required': Blocker.PERMISSION,
    'effect_unknown': Blocker.UNKNOWN_EFFECT, 'human_question': Blocker.USER_INPUT,
    'awaiting_input': Blocker.USER_INPUT, 'legacy_safety_review': Blocker.SAFETY_REVIEW,
    'safety_review_inconclusive': Blocker.SAFETY_REVIEW,
    'review_evidence_unavailable': Blocker.SAFETY_REVIEW,
})
REVIEW_BLOCKERS = frozenset({Blocker.NONE, Blocker.MISSING_AUTHORIZATION,
                           Blocker.INSUFFICIENT_EVIDENCE, Blocker.POLICY_VIOLATION, Blocker.SEMANTIC_RISK})


def canonical_blocker(kind):
    try:
        return ALIASES[kind] if kind in ALIASES else Blocker(kind)
    except ValueError as exc:
        raise KeyError(kind) from exc


def policy(kind):
    return REGISTRY[canonical_blocker(kind)]


# Derived compatibility views; callers cannot add a second source of policies.
BLOCKER_CLEARANCE = MappingProxyType({kind: policy(kind).clearance for kind in (*REGISTRY, *ALIASES)})
BLOCKER_CLASS = MappingProxyType({kind: policy(kind).category for kind in (*REGISTRY, *ALIASES)})
