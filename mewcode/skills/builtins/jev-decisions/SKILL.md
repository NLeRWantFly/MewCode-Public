---
name: jev-decisions
description: Use MewCode's Jev slots for option selection, recoverable blockers, bounded navigation and tool-history shadow inspection; design typed judgment plugins. Not a source of user facts, authorization, or independent acceptance.
---

# Jev decision partner

The main model frames the problem and explains/plans; Jev makes bounded typed
judgments; host code enforces constraints and executes. Keep the official
`typesafe-ai` skill unchanged. Use deterministic code for arithmetic and hard rules.

## Choosing and replanning

1. Inspect `ProbeCapabilities(target="decision")` in the current runtime. A
   registered tool is not proof of credentials or permission. Missing Jev routes
   fall back to main-model reasoning, not a mandatory user interruption.
2. For choices already within the task, call `DecideNextStep` with a stable
   `issue`, explicit goal, known facts/preferences, observed evidence references,
   and distinct candidates. Exclude candidates failing deterministic constraints.
3. `selected` is a suggestion, not the user's answer. Continue planning and use
   ordinary permission-checked tools. Do not describe the probabilities as proof.
4. `rethink` (including all candidates rejected): inspect evidence, find the
   failed assumption, propose materially different candidates or add new facts,
   then call again with the same issue. Renaming candidates is not replanning.
5. After two unsuccessful attempts, state what was tried and ask one targeted
   question about the concrete remaining blocker. Never repeat an uncertain write.

Do not ask the user to choose routine implementation details before trying this
loop. Do ask directly for genuinely missing preferences, secrets or authorization:
`AskUserQuestion(purpose="user_input" | "authorization")`. A technical choice can
also use `purpose="decision"`, options, known `decision_state`, and `decision_issue`;
the host consults Jev before displaying a question. Never disguise consent as an
ordinary decision to avoid permission checks.

Identical state/spec/model inputs reuse receipts. New facts permit a new judgment;
per-issue and per-conversation budgets remain in force across restarts. Cancellation
retains the reservation and never automatically resends an uncertain paid request.

## Browser reference plugin

Open a task page with `browser_navigate`, then `FastBrowse(goal, issue,
allowed_hosts, max_steps)` can follow observed links without a main-model call per
step. This initial adapter supports navigation only: no form filling, arbitrary
clicks, login, purchases, screenshots, canvas, uploads, or JavaScript execution.
It shares the browser serialization boundary and checks permission for each
underlying tool. Stale/unknown outcomes or bounded exhaustion return to the main
model. `observed_done` requires the main model to verify the returned evidence.

## Building another slot

Read [the contract](references/contract.md) when designing a new plugin. The mother
contract is `JudgmentSpec` with independent Choice/Noul/Score questions;
`DecisionSpec` remains the compatible selection adapter, not the universal shape.
Keep domain fallback in the consuming plugin. Selection can replan; an optional
context optimization failing must preserve content, not ask the user.
Do not implement a second provider client, permission gate, billing ledger or
universal ask-user loop.

## Tool-history shadow inspection

Use `ProbeCapabilities(target="context")` and `InspectToolHistory(action="report")`
to inspect this runtime's non-mutating experiment. Report-only costs no model call.
Read [shadow evaluation](references/tool-history-shadow.md) before requesting a
sample, interpreting savings/risk, or designing a later pruning implementation.
No production pruning mode exists in this build; do not edit configuration or
history to simulate activation. An estimated saving or Jev verdict cannot enable it.
