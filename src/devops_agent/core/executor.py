"""Execute a reviewed plan once, record outcomes, and verify observed URLs."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from devops_agent.core.approvals import ApprovalScope, require_approval
from devops_agent.core.diagnostics import Diagnostic, classify
from devops_agent.core.planner import DeploymentPlan
from devops_agent.core.security import SubprocessRunner, redact
from devops_agent.core.verifier import Verification, verify
from devops_agent.providers.base import ProviderStatus


@dataclass(frozen=True)
class DeploymentReport:
    project: str
    environment: str
    provider: str
    account: str | None
    phases: dict[str, str]
    release: str | None
    url: str | None
    verdict: str
    diagnostic: Diagnostic | None
    verifications: tuple[Verification, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "project": self.project,
            "environment": self.environment,
            "provider": self.provider,
            "account": self.account,
            "phases": self.phases,
            "release": self.release,
            "url": self.url,
            "verdict": self.verdict,
            "diagnostic": asdict(self.diagnostic) if self.diagnostic else None,
            "verifications": [item.to_dict() for item in self.verifications],
            "warnings": self.warnings,
        }


class AuditLog:
    def __init__(self, root: Path):
        self.folder = root / ".devops-agent"
        if self.folder.is_symlink():
            raise ValueError("Audit directory cannot be a symlink")
        self.folder.mkdir(mode=0o700, exist_ok=True)
        self.path = self.folder / "operations.jsonl"
        if self.path.is_symlink():
            raise ValueError("Audit log cannot be a symlink")

    def write(self, event: str, **fields: object) -> None:
        record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)


def execute(
    plan: DeploymentPlan,
    provider: object,
    status: ProviderStatus,
    runner: SubprocessRunner,
    *,
    approve: str | None = None,
    expected_account: str | None = None,
) -> DeploymentReport:
    if status.name != plan.provider or not status.authenticated or not status.installed:
        raise PermissionError("Provider identity or authentication changed since planning")
    if status.account != plan.account:
        raise PermissionError("Active provider account changed since planning")
    if expected_account is not None and status.account != expected_account:
        raise PermissionError(
            f"Active account {status.account!r} does not match requested account {expected_account!r}"
        )
    if plan.environment == "production" or plan.provider == "aws":
        require_approval(ApprovalScope("deploy", plan.provider, plan.project, plan.environment), approve)
    audit = AuditLog(runner.cwd)
    phases = {
        "preflight": "NOT_TESTED",
        "tests": "NOT_TESTED",
        "build": "NOT_TESTED",
        "deployment": "NOT_TESTED",
        "health": "NOT_TESTED",
    }
    warnings = list(plan.warnings)
    url: str | None = None
    release: str | None = None
    for spec in (*plan.preflight, *plan.build, *plan.deploy):
        phase = (
            "deployment"
            if spec.mutating
            else "preflight"
            if spec in plan.preflight
            else "tests"
            if spec.purpose.startswith("test")
            else "build"
        )
        audit.write(
            "command_start",
            provider=plan.provider,
            project=plan.project,
            environment=plan.environment,
            phase=phase,
            executable=Path(spec.argv[0]).name,
        )
        result = runner.run(spec)
        audit.write(
            "command_end",
            provider=plan.provider,
            project=plan.project,
            environment=plan.environment,
            phase=phase,
            returncode=result.returncode,
        )
        if spec.mutating and hasattr(provider, "parse_deployment"):
            result = provider.parse_deployment(result)
        if result.url:
            url = result.url
        if result.release:
            release = result.release
        if not result.ok:
            phases[phase] = "FAILED"
            return DeploymentReport(
                plan.project,
                plan.environment,
                plan.provider,
                status.account,
                phases,
                release,
                url,
                "FAILED",
                classify(result.stderr + "\n" + result.stdout, result.returncode),
                (),
                tuple(warnings),
            )
        phases[phase] = "PASSED"
    checks: tuple[Verification, ...] = ()
    targets = [url] if url else []
    if plan.environment == "production":
        targets.extend(target for target in plan.health_urls if target != url)
    if targets:
        checks = tuple(verify(target) for target in targets)
        phases["health"] = (
            "FAILED"
            if any(item.verdict == "FAILED" for item in checks)
            else "DEGRADED"
            if any(item.verdict == "DEGRADED" for item in checks)
            else "PASSED"
        )
    else:
        warnings.append("No release-specific deployment URL was available for verification")
    verdict = (
        "FAILED"
        if phases["health"] == "FAILED"
        else "DEGRADED"
        if phases["health"] == "DEGRADED"
        else "HEALTHY"
        if phases["health"] == "PASSED"
        else "UNVERIFIED"
    )
    audit.write(
        "release_verdict",
        provider=plan.provider,
        project=plan.project,
        environment=plan.environment,
        verdict=verdict,
    )
    return DeploymentReport(
        plan.project,
        plan.environment,
        plan.provider,
        status.account,
        phases,
        release,
        redact(url) if url else None,
        verdict,
        None,
        checks,
        tuple(warnings),
    )
