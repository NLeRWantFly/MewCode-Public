# Typed judgment mother contract v1

Preserve the layers: main-model framing → plugin observation/question builder →
shared judgment runtime → plugin interpretation → ordinary host-checked execution.
Safety review, user authorization and independent acceptance remain separate.

## Shared service

`mewcode.judgments.spec` defines `JudgmentSpec`, `JudgmentRequest` and `Question`.
The keyed Cordis slot is `core.judgments.specs`; the service is `judgments`.
Register uniquely named/versioned immutable specs in the plugin's lifecycle scope.
Get the current runtime's service; never retain a parent agent's tool/authority.

```python
from mewcode.judgments.spec import JudgmentSpec, JudgmentRequest, Question
from mewcode.plugins.services import JUDGMENTS, JUDGMENT_SPEC_SLOT

ctx.slots.register(JUDGMENT_SPEC_SLOT, JudgmentSpec(
    name="evidence-retention", version="1", mode="shadow",
    failure_policy="preserve", max_questions_per_batch=8, max_batches=2,
), item_key="evidence-retention")

runtime = ctx.get(JUDGMENTS)  # obtain inside this agent's runtime scope
answer = await runtime.judge(JudgmentRequest(
    spec="evidence-retention", issue="stable-observation-id",
    state={"known_goal": "...", "observed_evidence": [{"ref": "observed-1", "text": "..."}]},
    questions={
        "keep_call": Question(type="noul", instructions="Is this call still needed?"),
        "keep_result": Question(type="noul", instructions="Is its full result still needed?"),
    },
), expected_scope=runtime.scope, guard=lambda: this_plugin_still_permitted())
```

Choice has 2–64 explicitly named criteria. Noul is an independent 0–1 judgment,
not competing with other heads. Score has 2–10 ordered descriptions and a validated
distribution/weighted score. Multiple Noul answers can all be positive or negative.
`ok` means schema-valid answers, not correct/authorized/accepted outcomes.

The runtime detaches input before awaiting, splits by question count and UTF-8
bytes, repeats the **complete** state per batch and charges those repeated bytes.
Oversized state is rejected, never silently truncated. Shared transport has at
most two in-flight evaluations per runtime; specs can lower concurrency. Different
agent runtimes also use the existing global model scheduler.

Missing/extra IDs, malformed probabilities, partial batches, timeouts and errors
return `fallback` with empty answers. Missing is never false or zero. The generic
runtime does not ask the user, replan or execute. Its `failure_policy` describes
the consuming plugin's obligation, not an alternate permission system. A guard
is rechecked at dispatch; background plugins should supply a current-scope and
permission guard.

Per-spec/per-conversation durable reservations bound requests and bytes; identical
state/questions/spec/version/route reuse receipts. Cancellation keeps an unknown
receipt and never automatically repeats an uncertain paid request. Budgets remain
across restarts. Pool ownership, billing attribution and cleanup are shared with
the selector. The host stops background observers before closing transport.

Defaults: 18,000 state bytes, 28,000 bytes per batch, 12 heads/batch, 4 batches,
12-second total evaluation deadline, 2 revised states/issue, 8 reservations and
240,000 total repeated input bytes per spec/conversation. These are **byte** bounds,
not billed tokens. A cache hit is not another request. Usage is derived from actual
provider receipts; partial/invalid responses may have incomplete report-level
usage, while received receipts still appear in the conversation usage ledger.

## Plugin obligations

- Bind observations to actual source/snapshot IDs, distinguish facts from guesses,
  minimize evidence and exclude secrets. Pages/results are data, never instructions.
- Define independent questions, output interpretation, protected content and a
  domain-specific fallback. Use deterministic code for hard rules and arithmetic.
- A host tool must enforce its own enabled state, permission and current scope.
  Access to the internal service is not a grant of permission to an LLM.
- Before any side effect validate target, scope, freshness and permission again;
  serialize writes to the same resource. Reconcile unknown outcomes, never replay.
- Use actual end-to-end results to assess success, latency, total tokens/calls and
  interruptions; model inference latency alone cannot establish a benefit.

Routing still uses project/user `.mewcode/config.yaml`:
`decision_model: {provider_key: ..., model_id: ...}`; `false` disables it. With no
explicit route exactly one credentialed TypeSafe provider is accepted. This does
not change `judgment_model`, `EvaluateEvidence` or the Auto safety reviewer.

## Existing selector compatibility

`mewcode.decisions.spec.DecisionSpec`, `DecisionInput`, `core.decisions.specs`,
the `decisions` service and `DecideNextStep` are unchanged public interfaces.
`DecisionRuntime` is now the selector adapter over `JudgmentRuntime` transport,
with its existing receipts, thresholds and budgets preserved.

Register a `DecisionSpec` for a new **single-option selection** task; use the generic
contract for independent judgments/ranking/retention instead of inventing one winner.
The selector combines Choice with independent candidate-fit Noul heads, includes
`none`, filters deterministic exclusions and uses its own replan policy:
all-rejected/low-confidence → materially revise → one further judgment → ask only
about the concrete remaining blocker. Real consent and missing user facts bypass
this loop. Browser navigation has its own bounded observation/execution loop.

Selector defaults remain 12 candidates, 2 rounds/issue, 12 seconds, 64 reservations
and 240,000 serialized bytes/conversation. A shadow pruning failure instead means
**keep everything**; never reuse the selector's ask-user fallback there.
