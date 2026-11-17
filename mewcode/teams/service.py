"""Host-facing planning facade for opt-in teams; no execution side effects."""
from __future__ import annotations

from mewcode.mcp.coord_store import CoordinationStore
from .spec import CompiledTeam, SpecRef, TeamSpec
from .spec_compiler import CompilationContext, compile_team_spec
from .store_v2 import TeamActor, TeamStateError


class TeamService:
    def __init__(self, store: CoordinationStore, *, actor: TeamActor,
                 compilation_context: CompilationContext) -> None:
        self.repository = store.teams_v2
        self.actor = actor
        self.context = compilation_context

    def validate_spec(self, spec: TeamSpec | dict) -> CompiledTeam:
        self.actor.require("team.plan")
        return compile_team_spec(spec, catalog=self.repository.spec_catalog(self.actor), context=self.context)

    def register_spec(self, spec: TeamSpec | dict, *, command_id: str) -> dict:
        compiled = self.validate_spec(spec)
        return self.repository.register_spec(self.actor, compiled.spec, command_id=command_id)

    def create_team(self, ref: SpecRef, *, name: str, command_id: str) -> dict:
        return self.repository.create_team(self.actor, ref, name=name, command_id=command_id)

    def plan_run(self, team_id: str, *, objective: str, budget_ref: str,
                 expected_version: int, control_epoch: int, command_id: str) -> dict:
        team = self.repository.get_team(self.actor, team_id)
        catalog = self.repository.spec_catalog(self.actor)
        spec = next((entry for entry in catalog if (entry.spec_id, entry.version) == (
            team["spec_id"], team["spec_version"])), None)
        if spec is None:
            raise TeamStateError("UNKNOWN_SPEC", team["spec_id"])
        compiled = compile_team_spec(spec, catalog=catalog, context=self.context)
        return self.repository.save_draft_run(
            self.actor, team_id, compiled, objective=objective, budget_ref=budget_ref,
            expected_version=expected_version, control_epoch=control_epoch, command_id=command_id)
