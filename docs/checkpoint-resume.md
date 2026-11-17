# Checkpoint continuation and review evidence

Ordinary **Continue** resumes the current invocation. It does not invalidate
an initial review because that review submitted `NEEDS_ATTENTION`. Completed
findings are inputs to repair, not an instruction to restart the graph.
Messages, pending questions, recorded file operations and bounded attempt
memory survive continuation. Legacy acceptance-reset archives contribute
failure/edit summaries only, never old approvals or executable tool calls.

**Retry node** is explicit. It retains prior work and failure memory but
invalidates downstream results that depend on the retried work, including
old acceptance. Candidate recovery and loops retain their own checkpoint and
budget controls. A finished failed acceptance with nothing left to continue
exposes Retry instead of silently re-running the initial audit.

## Conditional routing is not the data contract

The scheduler still follows only activated graph edges. Node input assembly
also follows their activated ancestors, so an intermediate decision cannot
erase a planner's handoff or the initial review's issue list. Inactive sibling
branches are not included. `ReadStageResult` provides character-paginated,
invocation-scoped access to the complete records without arbitrary access to
other nodes or runs. Its explicit tool allow/deny controls remain effective.

The runtime supplies the actual run ID, flow version and dispatched model.
These facts are not a future availability guarantee, user consent, or new
permission. Old planning-version prose does not override runtime identity.

## Review receipts and completion

An ordinary structured audit gets an immutable receipt containing its verdict,
run, node, flow version and a deterministic `snapshot_hash`. It does not invent
a quantified audit's `rubric_hash` or approve anything.

Final `SubmitVerdict(PASS)` must cite the exact upstream receipt hashes in
`reviewed_snapshots` (`node_id -> snapshot_hash`) after checking all their
findings. A missing/stale reference is rejected. Only this proof can discharge
the initial findings; an unrelated PASS or a manager summary cannot. The
original verdict remains unchanged, with a display-only discharge annotation.

`SubmitHandoff.status` reports actual stage completion, not whether an answer
ended. Blockers, missing/empty local artifacts and a blocked delivery-status
record prevent a completed claim. File checks do not establish semantic
correctness: independent acceptance is still required.

On resume, unchanged human questions are replayed with their original IDs.
Changed input facts cause a model question to be reassessed with an explicit
"not answered, not authorized" reminder. Permission requests always retain
their original approval path; context updates never grant permission.

## Visual evidence

Read-only roles can use `ReadAuditArtifact` for PNG/JPEG and PDF pages.
PDF layout checks require `render_page=true`; text extraction is not visual
proof. Rendering is bounded to one page, 2,400 pixels on the long edge and
four million pixels total, without output files or model-selected commands.
Non-vision models fail explicitly. PDFium access is serialized according to
its [threading and memory rules](https://pypdfium2.readthedocs.io/en/stable/python_api.html).
The dependency and native library are included in the desktop build.

Regression coverage lives in `test_findings_resume.py`, `test_human_waits.py`,
`test_read_audit_artifact.py`, `test_acceptance_completion.py` and the isolated
hidden renderer `expert_calls_dom.cjs`. These tests do not resume a user's
actual task or call a model provider.
