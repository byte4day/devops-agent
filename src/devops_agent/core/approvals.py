"""Target-bound authorization for high impact operations."""

from __future__ import annotations

from dataclasses import dataclass


class ApprovalRequired(PermissionError):
    pass


@dataclass(frozen=True)
class ApprovalScope:
    operation: str
    provider: str
    project: str
    environment: str

    @property
    def phrase(self) -> str:
        return f"{self.operation}:{self.provider}:{self.project}:{self.environment}"


def require_approval(scope: ApprovalScope, supplied: str | None) -> None:
    if supplied != scope.phrase:
        raise ApprovalRequired(
            f"Approval required for {scope.operation} on {scope.provider}/{scope.project}/{scope.environment}. "
            f"After reviewing the plan, pass --approve '{scope.phrase}'"
        )
