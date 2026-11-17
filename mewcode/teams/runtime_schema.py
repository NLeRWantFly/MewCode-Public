"""Transactional schema additions for the existing coordination database."""

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS team_v2_runtime_schema(version INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_v2_runtime_runs(
        team_run_id TEXT PRIMARY KEY REFERENCES team_v2_runs(team_run_id),
        state TEXT NOT NULL, state_version INTEGER NOT NULL DEFAULT 1,
        bindings_json TEXT NOT NULL, budget_ref TEXT NOT NULL, input_json TEXT NOT NULL,
        export_json TEXT, question_json TEXT, task_set_revision INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS team_v2_budgets(
        project_id TEXT NOT NULL, budget_ref TEXT NOT NULL, limit_tokens INTEGER NOT NULL,
        used_tokens INTEGER NOT NULL DEFAULT 0, reserved_tokens INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(project_id,budget_ref))""",
    """CREATE TABLE IF NOT EXISTS team_v2_tasks(
        task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), team_id TEXT NOT NULL,
        team_run_id TEXT NOT NULL REFERENCES team_v2_runs(team_run_id), task_key TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, contract_version INTEGER NOT NULL DEFAULT 1,
        input_revision INTEGER NOT NULL DEFAULT 1, state_version INTEGER NOT NULL DEFAULT 1,
        fence INTEGER NOT NULL DEFAULT 0, spent_tokens INTEGER NOT NULL DEFAULT 0,
        held_tokens INTEGER NOT NULL DEFAULT 0, token_limit INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, question_json TEXT, block_reason TEXT,
        UNIQUE(team_run_id,task_key))""",
    """CREATE TABLE IF NOT EXISTS team_v2_task_revisions(
        task_id TEXT NOT NULL REFERENCES tasks(task_id), revision INTEGER NOT NULL,
        contract_version INTEGER NOT NULL, input_revision INTEGER NOT NULL,
        contract_json TEXT NOT NULL, input_hash TEXT NOT NULL, state TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(task_id,revision))""",
    """CREATE TABLE IF NOT EXISTS team_v2_inputs(
        task_id TEXT NOT NULL, revision INTEGER NOT NULL, port_id TEXT NOT NULL,
        schema_ref TEXT NOT NULL, required INTEGER NOT NULL, source_task_id TEXT,
        source_port TEXT, version_id TEXT, receipt TEXT NOT NULL DEFAULT 'pending', reason TEXT,
        PRIMARY KEY(task_id,revision,port_id),
        FOREIGN KEY(task_id,revision) REFERENCES team_v2_task_revisions(task_id,revision))""",
    """CREATE TABLE IF NOT EXISTS team_v2_attempts(
        attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
        revision INTEGER NOT NULL, fence INTEGER NOT NULL, control_epoch INTEGER NOT NULL,
        principal_ref TEXT NOT NULL, worker_session_id TEXT NOT NULL, token_hash TEXT NOT NULL,
        lease_until REAL NOT NULL, state TEXT NOT NULL, reserved_tokens INTEGER NOT NULL,
        usage_tokens INTEGER, input_hash TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(task_id,fence))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS team_v2_one_active_attempt ON team_v2_attempts(task_id)
        WHERE state='running'""",
    """CREATE TABLE IF NOT EXISTS team_v2_artifacts(
        version_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, team_id TEXT NOT NULL,
        team_run_id TEXT NOT NULL REFERENCES team_v2_runs(team_run_id), producer_task_id TEXT,
        port_id TEXT NOT NULL, schema_ref TEXT NOT NULL, media_type TEXT NOT NULL,
        content BLOB NOT NULL, content_hash TEXT NOT NULL, state TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_v2_artifact_grants(
        version_id TEXT NOT NULL REFERENCES team_v2_artifacts(version_id),
        recipient_run_id TEXT NOT NULL REFERENCES team_v2_runs(team_run_id),
        granted_by TEXT NOT NULL, PRIMARY KEY(version_id,recipient_run_id))""",
    """CREATE TABLE IF NOT EXISTS team_v2_handoffs(
        handoff_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
        revision INTEGER NOT NULL, attempt_id TEXT NOT NULL UNIQUE REFERENCES team_v2_attempts(attempt_id),
        packet_json TEXT NOT NULL, packet_hash TEXT NOT NULL, state TEXT NOT NULL,
        input_hash TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_v2_reviews(
        review_id TEXT PRIMARY KEY, handoff_id TEXT NOT NULL REFERENCES team_v2_handoffs(handoff_id),
        actor_ref TEXT NOT NULL, accepted INTEGER NOT NULL, review_json TEXT NOT NULL,
        review_hash TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_v2_receipts(
        receipt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
        port_id TEXT NOT NULL, version_id TEXT NOT NULL REFERENCES team_v2_artifacts(version_id),
        actor_ref TEXT NOT NULL, accepted INTEGER NOT NULL, reason TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(task_id,revision,port_id,version_id))""",
    """CREATE TABLE IF NOT EXISTS team_v2_messages(
        message_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, team_run_id TEXT NOT NULL,
        sender_ref TEXT NOT NULL, recipient_ref TEXT NOT NULL, body TEXT NOT NULL,
        created_at TEXT NOT NULL, acked_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS team_v2_publications(
        publication_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, team_run_id TEXT NOT NULL,
        task_id TEXT, control_epoch INTEGER NOT NULL, state TEXT NOT NULL,
        journal_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS team_v2_workspace_heads(
        workspace TEXT NOT NULL, relative_path TEXT NOT NULL, generation INTEGER NOT NULL,
        PRIMARY KEY(workspace,relative_path))""",
    """CREATE TABLE IF NOT EXISTS team_v2_plan_revisions(
        team_run_id TEXT NOT NULL REFERENCES team_v2_runs(team_run_id), revision INTEGER NOT NULL,
        compiled_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(team_run_id,revision))""",
    """CREATE TABLE IF NOT EXISTS team_v2_questions(
        question_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
        revision INTEGER NOT NULL, body_json TEXT NOT NULL, state TEXT NOT NULL,
        answer TEXT, created_at TEXT NOT NULL)""",
)
