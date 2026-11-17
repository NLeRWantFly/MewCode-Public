"""Additive responsibility state in the existing coordination transaction domain."""

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS team_v2_missions(
        team_run_id TEXT PRIMARY KEY REFERENCES team_v2_runs(team_run_id),
        version INTEGER NOT NULL, owner_ref TEXT NOT NULL, contract_json TEXT NOT NULL,
        policy_json TEXT NOT NULL, state_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_v2_mission_revisions(
        team_run_id TEXT NOT NULL, version INTEGER NOT NULL, contract_json TEXT NOT NULL,
        actor_ref TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(team_run_id,version))""",
    """CREATE TABLE IF NOT EXISTS team_v2_responsibilities(
        team_run_id TEXT NOT NULL, task_key TEXT NOT NULL, goal_version INTEGER NOT NULL,
        assignment_json TEXT NOT NULL, PRIMARY KEY(team_run_id,task_key))""",
    """CREATE TABLE IF NOT EXISTS team_v2_goal_reviews(
        review_id TEXT PRIMARY KEY, team_run_id TEXT NOT NULL, goal_version INTEGER NOT NULL,
        plan_revision INTEGER NOT NULL, state_hash TEXT NOT NULL, accepted INTEGER NOT NULL,
        scores_json TEXT NOT NULL, actor_ref TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_v2_disputes(
        dispute_id TEXT PRIMARY KEY, team_run_id TEXT NOT NULL, task_id TEXT NOT NULL,
        issue_key TEXT NOT NULL, criterion_id TEXT NOT NULL, owner_role TEXT NOT NULL,
        state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        submission_json TEXT NOT NULL, outcome_json TEXT,
        rounds INTEGER NOT NULL DEFAULT 0, no_progress_rounds INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(team_run_id,task_id,criterion_id,issue_key))""",
    """CREATE TABLE IF NOT EXISTS team_v2_dispute_evidence(
        evidence_id TEXT PRIMARY KEY, dispute_id TEXT NOT NULL REFERENCES team_v2_disputes(dispute_id),
        identity_hash TEXT NOT NULL, actor_ref TEXT NOT NULL, evidence_json TEXT NOT NULL,
        verified_by TEXT, verified_at TEXT, created_at TEXT NOT NULL,
        UNIQUE(dispute_id,identity_hash))""",
    """CREATE TABLE IF NOT EXISTS team_v2_dispute_rounds(
        dispute_id TEXT NOT NULL, round INTEGER NOT NULL, evidence_ids_json TEXT NOT NULL,
        action TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(dispute_id,round))""",
    """CREATE TABLE IF NOT EXISTS team_v2_evidence_attestations(
        evidence_id TEXT NOT NULL REFERENCES team_v2_dispute_evidence(evidence_id),
        task_revision INTEGER NOT NULL, verified_by TEXT NOT NULL,
        verification TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(evidence_id,task_revision))""",
    """CREATE TABLE IF NOT EXISTS team_v2_interventions(
        intervention_id TEXT PRIMARY KEY, team_run_id TEXT NOT NULL, task_id TEXT,
        sender_ref TEXT NOT NULL, intent TEXT NOT NULL, state TEXT NOT NULL,
        request_json TEXT NOT NULL, receipt_json TEXT NOT NULL, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_v2_intervention_deliveries(
        intervention_id TEXT NOT NULL, attempt_id TEXT NOT NULL, task_id TEXT NOT NULL,
        revision INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(intervention_id,attempt_id))""",
    """CREATE TABLE IF NOT EXISTS team_v2_plan_proposals(
        proposal_id TEXT PRIMARY KEY, team_run_id TEXT NOT NULL, goal_version INTEGER NOT NULL,
        base_plan_revision INTEGER NOT NULL, base_plan_hash TEXT NOT NULL,
        compiled_json TEXT NOT NULL, bindings_json TEXT NOT NULL, assignments_json TEXT NOT NULL,
        impact_json TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
        proposed_by TEXT NOT NULL, created_at TEXT NOT NULL, applied_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS team_v2_superseded_tasks(
        task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), plan_revision INTEGER NOT NULL,
        replacement_keys_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_v2_model_preferences(
        team_run_id TEXT NOT NULL, role_id TEXT NOT NULL, preference_json TEXT NOT NULL,
        PRIMARY KEY(team_run_id,role_id))""",
    """CREATE TABLE IF NOT EXISTS team_v2_model_routes(
        project_id TEXT NOT NULL, route_id TEXT NOT NULL, descriptor_json TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
        PRIMARY KEY(project_id,route_id))""",
    """CREATE TABLE IF NOT EXISTS team_v2_run_deployments(
        team_run_id TEXT PRIMARY KEY, deployment_json TEXT NOT NULL, version INTEGER NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_v2_resource_grants(
        team_run_id TEXT NOT NULL, resource_id TEXT NOT NULL, grant_json TEXT NOT NULL,
        version INTEGER NOT NULL, PRIMARY KEY(team_run_id,resource_id))""",
    """CREATE TABLE IF NOT EXISTS team_v2_resource_leases(
        project_id TEXT NOT NULL, resource_key TEXT NOT NULL, attempt_id TEXT NOT NULL,
        task_id TEXT NOT NULL, mode TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(project_id,resource_key,attempt_id))""",
    """CREATE TABLE IF NOT EXISTS team_v2_tool_calls(
        attempt_id TEXT NOT NULL, call_id TEXT NOT NULL, task_id TEXT NOT NULL,
        revision INTEGER NOT NULL, request_hash TEXT NOT NULL, name TEXT NOT NULL,
        resource_id TEXT NOT NULL, state TEXT NOT NULL, request_json TEXT NOT NULL,
        result_json TEXT, started_at TEXT NOT NULL, finished_at TEXT,
        PRIMARY KEY(attempt_id,call_id))""",
    """CREATE TABLE IF NOT EXISTS team_v2_coordinator_turns(
        turn_id TEXT PRIMARY KEY, team_run_id TEXT NOT NULL, goal_version INTEGER NOT NULL,
        plan_revision INTEGER NOT NULL, state TEXT NOT NULL, input_hash TEXT NOT NULL,
        decision_json TEXT, result_json TEXT, created_at TEXT NOT NULL, finished_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS team_v2_attempt_contexts(
        attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, goal_version INTEGER,
        context_json TEXT NOT NULL, context_hash TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_v2_model_turns(
        attempt_id TEXT NOT NULL, turn INTEGER NOT NULL, request_hash TEXT NOT NULL,
        state TEXT NOT NULL, response_json TEXT, usage_tokens INTEGER,
        created_at TEXT NOT NULL, finished_at TEXT, PRIMARY KEY(attempt_id,turn))""",
    """CREATE TABLE IF NOT EXISTS team_v2_coord_advice(
        advice_id TEXT PRIMARY KEY, team_run_id TEXT NOT NULL, task_id TEXT NOT NULL,
        revision INTEGER NOT NULL, goal_version INTEGER NOT NULL,
        advice_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_v2_coord_model_calls(
        call_id TEXT PRIMARY KEY, team_run_id TEXT NOT NULL, route_id TEXT NOT NULL,
        control_epoch INTEGER NOT NULL, goal_version INTEGER NOT NULL, plan_revision INTEGER NOT NULL,
        state TEXT NOT NULL, reserved_tokens INTEGER NOT NULL, usage_tokens INTEGER,
        request_hash TEXT NOT NULL, response_json TEXT, created_at TEXT NOT NULL, finished_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS team_v2_host_jobs(
        job_id TEXT PRIMARY KEY, team_run_id TEXT NOT NULL, control_epoch INTEGER NOT NULL,
        state TEXT NOT NULL, max_steps INTEGER NOT NULL, result_json TEXT,
        created_at TEXT NOT NULL, finished_at TEXT)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS team_v2_one_host_job
        ON team_v2_host_jobs(team_run_id) WHERE state IN ('queued','running')""",
    """CREATE UNIQUE INDEX IF NOT EXISTS team_v2_one_coordinator_turn
        ON team_v2_coordinator_turns(team_run_id) WHERE state='running'""",
    """CREATE INDEX IF NOT EXISTS team_v2_dispute_inbox ON team_v2_disputes(team_run_id,state)""",
    """CREATE INDEX IF NOT EXISTS team_v2_intervention_inbox ON team_v2_interventions(team_run_id,state)""",
)
