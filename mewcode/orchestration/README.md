# Orchestration graph

Product path: **propose → bind models → confirm → run**.

- `spec.py` — `plan|implement|reduce|audit|gate` nodes
- `templates.py` — builtin templates (linear / fe+be parallel / classic four-role)
- `propose.py` — goal → draft spec (template heuristics; optional raw planner JSON)
- `convert.py` — `OrchestrationSpec` ↔ `ExpertFlowSpec` for existing `_run_graph`
- `validate.py` — DAG + binding checks

Plugin slots (keyed):

- `core.orchestration.templates`
- `core.orchestration.nodes`
- `core.orchestration.gates`

HTTP:

- `GET /api/orchestration/templates`
- `POST /api/orchestration/propose`
- `POST /api/orchestration/confirm`
- `GET /api/orchestration/current`
