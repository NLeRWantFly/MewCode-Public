"""Opt-in adapter for testing the retired reviewer, never a runtime default."""
from dataclasses import replace
from mewcode.permissions import PermissionChecker


class LegacyReviewChecker(PermissionChecker):
    def check(self, tool, arguments):
        decision = super().check(tool, arguments)
        if decision.source in {"auto_escalation", "native_boundary_unverified"}:
            # Reviewer recovery tests use mocked command tools to exercise
            # the retired review protocol. Production PermissionChecker still
            # refuses AUTO commands before review until the OS boundary passes.
            return replace(decision, effect="review", source="auto_escalation_review")
        return decision
