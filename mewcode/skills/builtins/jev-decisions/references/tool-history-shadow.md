# Tool-history pruning: shadow-only experiment

The plugin `core.tool_history_shadow` registers `tool-history-pruning` in the typed
judgment slot. Its runtime service is `tool_history_shadow`; the public tool is
`InspectToolHistory`. Both chat and graph runtimes create local observers. A node
that omits/disables the tool cannot start a background judgment. The native
permission check must return allow; ask/review/deny causes a silent skip.

## Behavior

The observer sees `agent/before_model.messages`, **after** existing tool-result
budgets/replacements. It does not operate on the raw transcript, mutate messages,
rewrite tool-call/result pairs, alter cache anchors, persist context replacements
or change summary/compaction behavior. Jev runs in a bounded background task, not
as an awaited network step before the main model. Snapshot work still costs local
CPU and judgment calls still cost money/compete for scheduler capacity.

Protect first/recent messages, unpaired/duplicate/out-of-order IDs, errors and
unknown outcomes, credentials/authorization-related data, declared-sensitive
tools, writes/commands/unknown tools, multimodal/already-replaced results and
oversized inputs. Only bounded older, read-only candidates are considered.
User text constraints are never silently clipped; oversized or multimodal
constraints skip the sample. Recognizable credential patterns are excluded or
redacted, but this is **not** a general-purpose secret/DLP guarantee.

Each candidate has independent `keep_call` and `keep_result` Noul heads. Only strong
negative outputs propose `drop_call`; strong call retention plus strong result
rejection can propose `drop_result`. Otherwise keep, including any uncertain,
missing, invalid or partial answer. These are metadata suggestions, not an applied
projection. Excerpts and omitted surrounding/system context limit reliability.

Default: `shadow`, ≥8,000 eligible result characters, 6 recent messages protected,
≤8 candidates/sample, ≥30 seconds between samples, ≤4 paid snapshot reservations
and 160,000 repeated input bytes per conversation. Each sample has ≤2 batches,
≤2 concurrent requests, and a 12-second judgment deadline. Restart/cache reuse
does not create another paid reservation. Slot unload/config disable/tool denial
prevents subsequent dispatch; an already-sent call cannot be unsent.

Optional project `.mewcode/config.local.yaml`, project `config.yaml`, or user
`.mewcode/config.yaml` (first explicit setting wins):

```yaml
tool_history_pruning:
  mode: "shadow"  # only "off" or "shadow" exist
  min_candidate_chars: 8000
  preserve_recent_messages: 6
  max_candidates: 8
  sample_interval_seconds: 30
```

Invalid/`enforce` config disables the experiment and preserves content. Missing
Jev configuration silently skips; there is no fallback model or user question.
The TypeSafe `decision_model` route/credentials are reused, not copied.

## Inspection and evidence

- `ProbeCapabilities(target="context")`: loaded/enabled status, readiness and caps;
  even `live=true` cannot run a paid judgment or prune anything.
- `InspectToolHistory(action="report")`: current conversation's latest metadata
  report and aggregates, no model call. `sample` can schedule only the last observed
  provider snapshot, subject to the same permissions, cooldown, size and quotas;
  it never reads raw history. A report with zero samples is not a measured success.
- Reports store hashes/counts/proposals, not source arguments, transcript or result
  excerpts. They share the existing user-data `decisions-v1.sqlite3` file, under
  separate shadow tables and originating-conversation scope.

`hypothetical_saved_tokens_estimate` is characters/3.5, **not actual** billed tokens.
`drop_result` uses a hypothetical 700-character retained excerpt; no excerpt is
actually generated/applied. Overlapping snapshots cannot be added together.
`context_tokens_estimate` covers observed conversation messages, not the whole
provider request/system/tool schemas. `actual_saved_tokens` is always 0.
Track snapshot CPU latency, judgment latency, provider-reported tokens/calls and
`usage_complete`. The conversation's model statistics remain the billing-receipt
source, including received invalid-verdict receipts; failures can lack usage.

A later exact same-tool/same-arguments read after a proposed removal is a **risk
signal**, not proof of misdeletion. It cannot measure implicit dependencies, reads
with changed arguments or true downstream failure. No labels means unknown risk,
never a 0% error claim.

## Independent offline evaluation

Export the tool report JSON and independently label anonymous candidate refs from
the original local transcript (refs are the hash of tool ID/call index/result
index). Label the **specific proposed action**: `retain` = this removal would lose
required information; `dispensable` = this proposed removal is safe; otherwise
`unknown`. For `drop_result`, do not confuse needing the call with needing its full
result. The same Jev verdict being tested is not ground truth.

```console
python -m mewcode.context.shadow_store report.json independent-labels.json
```

The read-only scorer reports reviewed/unreviewed proposals, mistaken removals and
their rate among reviewed proposals. It never changes configuration or enables
pruning. Missing labels remain unknown; selective review is not a representative
error-rate estimate. Use representative cases, especially failures, parallel call
pairs, changing goals, credentials, write history and essential older evidence.

Promotion requires separate offline replay/A-B evaluation: unchanged task success
and protocol/cache integrity; acceptable independently assessed misdeletion risk;
positive end-to-end benefit after judgment overhead, retries, rereads and cache
effects. Establish representative sample coverage and error tolerance first.
Then request an explicitly approved live implementation with rollback. This build
has **no live-pruning path** and cannot auto-promote on any numeric threshold.
