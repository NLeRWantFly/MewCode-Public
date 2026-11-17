# Workflow checkpoint liveness

These rules apply to all structured loops, not just the document stage-five flow.

- A saved result is reusable only if completed/skipped, unblocked, and (for a reviewer) accompanied by a structured loop decision. Unfinished producers invalidate dependent results, including nested loops, but preserve independent completed nodes and artifacts.
- Explicit Continue repairs legacy checkpoints whose outer run is paused but inner loop still says running. Frozen contracts, user gates, original token/time accounting and strict resource limits remain authoritative.
- Save the latest segment and durably flush blocked loop state before awaiting a recovery dialogue. Cancelling a dialogue is not necessarily a user-requested task pause.
- `SubmitHandoff(status="needs_attention", blocker_kind="internal_work", open_items=[...])` means this node has more authorized work. It cannot include blockers, questions or capability-transfer requests. A downstream review/user gate is not an external dependency of a completed producer.
- Team mode can continue batches while host-persisted review records or submitted candidate item IDs advance. Monotonic high-water marks prevent regressed/repeated evidence from purchasing retries. Text, repeated reads and spending tokens are not progress. New work does not override permission requests, unknown effects or business questions.
- Missing reviewer decisions receive one bounded correction per round, within remaining resources. Natural language, UNKNOWN and incomplete work never become PASS automatically.
- Strict mode keeps its original limits. No-progress loops stop with an actionable reason; Auto does not mean infinite retries or bypassing approval.

Regression coverage: `tests/test_loop_checkpoint_liveness.py`, alongside the team-loop, generic-loop, scheduler-recovery, nested-loop and strict-budget suites. Tests use simulated role results; they prove scheduling behavior, not the correctness of business artifacts.

## Version-bound design diagnosis

Use `AdvanceTaskDesign(action="diagnose")` after a construction failure. This is a read-only operation, including when no task database exists. The task view, workflow-material header and the safety review's host task scope share the derived `mewcode.design-health.v1` report. No separate diagnostic database is authoritative.

The report binds the task revision, material sequence and document digest, candidate identity, baseline digest and checked runtime code. It validates the current effective materials, reports all invalid input collection targets before projection stops at its first error, and separates warnings, blockers and skipped checks. A reference-only pass or validation of an old candidate is not a current full structural pass. Historical failures remain recorded; a changed basis does not grant a retry or clear a semantic denial.

Recovery follows `next_action`: load changed runtime code, repair the current material fields, inspect a failure on the same basis, or review the current basis through the existing boundary. A repair hint may name the same input's prior valid candidate target, but is neither applied nor treated as authority. Existing snapshot/CAS checks govern all writes. Missing permissions continue through their specific host authorization flow.

`RuntimeDoctor.inspect` also returns `application_revision`. It compares selected **loaded function code** with compiled disk code without executing the disk source, importing replacements, reloading or restarting. Comments and line-number changes alone are not implementation changes. Missing/unverifiable symbols are reported as unknown, not current; this is not a whole-installation attestation. A detected mismatch blocks a new design queue request until runtime code is reconciled.

Every completed safety review retains an `input_receipt` in its existing audit route. It hashes the actual host input envelope and authorization context and records the workflow report identity/runtime fingerprint when present. The receipt contains no raw source text. It proves what the host prepared for that review, not that a provider understood it or approved business execution. A changed diagnostic basis invalidates the existing exact-context review cache; the new review can still deny.

Regression coverage: `tests/test_design_health.py` and `tests/orch_design_health.cjs`, plus the task-design, graph readiness, review-protocol and workbench suites. No test result alone proves the running desktop process loaded new source.

## Large original session histories

Source validation scans the complete original JSONL file in segments of at most
100 MiB, using IO buffers no larger than 1 MiB. The 100 MiB constant is unchanged:
it bounds each scan segment, each individual record, and the total retained
source evidence (including human-question context and parse warnings). The total
raw file may span multiple segments; it is no longer treated as one oversized
read. Tool logs and compaction records are scanned under the same bounds without
being promoted to user requirements. Original files are never truncated,
rewritten, archived or replaced by summaries.

Line numbers, source IDs, exact quotations and human-question pairing continue
across segments. No intermediate segment is a complete source snapshot. The
completed scan records its byte count, segment count and full raw SHA-256 digest.
A normal concurrent append is accepted only after rereading and verifying the
entire captured prefix in bounded reads; appended records belong to the next
snapshot. A replaced, shortened or changed prefix is rejected. Existing save
checks still reread sources and validate semantic fingerprints immediately
before committing, so new requirements and changed original sources invalidate
stale submissions. Malformed or incomplete records remain explicit warnings
that prevent a save; oversized individual records or retained evidence fail
without silently dropping history.

Regression coverage: `tests/test_history_scan.py` includes a real file exceeding
100 MiB, exact source identity across segment boundaries, concurrent changes,
resource bounds and material-save/CAS checks. This is segmented scanning within
one source-read operation, not a persistent index or an automatic task retry.
