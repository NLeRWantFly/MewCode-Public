"""Conservative ready-frontier selection; tool/model calls keep scheduler limits."""

READ_ONLY = frozenset({"ReadFile", "Glob", "Grep", "InspectGitState", "ReadAuditArtifact",
    "ReadStageResult", "WebSearch", "web_search", "WebFetch", "web_fetch",
    "AgentStatus", "ProbeCapabilities", "SubmitHandoff", "FindSkills", "LoadSkill"})


def expansion_wave(ready, definitions, *, concurrency=4):
    safe = [key for key in ready if not definitions[key].get("write_paths")
            and set(definitions[key].get("tools") or []) <= READ_ONLY]
    return safe[:concurrency] if len(safe) > 1 else ready[:1]


def allocate_wave_tokens(remaining, floors):
    if remaining is None:
        return None
    # Saved completed children also cost tokens after restart. Reserve their
    # receipts before dividing the rest; insufficient budgets grant no new work.
    if sum(floors) > remaining:
        return [0] * len(floors)
    share, remainder = divmod(max(0, remaining - sum(floors)), len(floors))
    return [floor + share + (i < remainder) for i, floor in enumerate(floors)]
