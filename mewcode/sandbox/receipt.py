"""Host execution facts, kept separate from arbitrary child output.

These receipts are diagnostics, not authorizations or sandbox attestations.
A child printing a success marker (or launcher echoing a command) cannot turn
a startup failure, timeout or missing observation into an isolation pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


class NativeCleanupUnconfirmed(RuntimeError):
    """The owner must stop and cannot hand off its writes as cleanly settled."""


@dataclass(frozen=True)
class NativeExecutionReceipt:
    status: Literal["denied", "spawn_failed", "timed_out", "cancelled", "exited"]
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    detail: str = ""
    diagnostic_only: bool = False
    backend: str = "codex-windows-elevated"
    cleanup_confirmed: bool | None = None
    output_truncated: bool = False
    transaction_id: str = ""
    publication_state: str = ""
    command_started: bool | None = None
    error_code: str = ""
    network_policy: dict | None = None

    @property
    def execution_started(self) -> bool | None:
        return True if self.status == "exited" else self.command_started

    def confirms_stdout_line(self, expected: str) -> bool:
        """Only successful executions with an exact stdout line are evidence."""
        return (
            not self.is_error
            and self.stdout.splitlines().count(expected) == 1
            and not self.output_truncated
        )

    def render(self) -> str:
        from mewcode.sandbox.execution_conditions import classify_execution_condition
        condition = classify_execution_condition(self)
        if condition["kind"] == "environment_unavailable":
            return f"AUTO_ENVIRONMENT_UNAVAILABLE: {self.detail}"
        if self.status != "exited" and self.execution_started is not True and self.exit_code is None:
            return f"AUTO_BOUNDARY_DENIED: {self.detail}"
        return (
            (f"AUTO_BOUNDARY_DENIED: {self.detail}\n" if self.status != "exited" else "")
            + f"ISOLATION_CANDIDATE: {self.backend}; network-requested="
            + ('enabled; ' if self.network_policy and self.network_policy.get('enabled') else 'disabled; ')
            + f"diagnostic_only={str(self.diagnostic_only).lower()}\n"
            f"cleanup_confirmed={self.cleanup_confirmed}; output_truncated={self.output_truncated}\n"
            f"transaction={self.transaction_id}; publication={self.publication_state or 'not_applicable'}\n"
            f"STDOUT:\n{self.stdout}\nSTDERR:\n{self.stderr}\nEXIT CODE: {self.exit_code}"
        )

    @property
    def is_error(self) -> bool:
        return (self.status != "exited" or self.exit_code != 0
                or bool(self.transaction_id and self.publication_state not in {"published", "no_changes", "discarded"}))
