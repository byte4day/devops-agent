"""GitHub Actions inspection through the official gh CLI."""

from __future__ import annotations

import json
import re
from typing import Any

from .base import BaseProvider, CommandSpec, ProviderStatus, Runner, UnsupportedOperation, root_of

_RUN_ID = re.compile(r"^[1-9][0-9]*$")


class Provider(BaseProvider):
    name = "github"
    binary = "gh"
    capabilities = ("ci_status", "ci_failed_logs", "workflow_inspection")
    login_hint = "gh auth login"

    def detect(self, runner: Runner) -> ProviderStatus:
        version = self._version(runner)
        if version is None:
            return self._status(None, False, detail="GitHub CLI is unavailable")
        result = self._read(runner, "auth", "status", "--active", "--json", "hosts")
        if not result.ok:
            return self._status(version, False, detail=self.login_hint)
        try:
            data = json.loads(result.stdout)
        except ValueError:
            return self._status(version, False, detail="gh authentication response could not be parsed")
        hosts = data.get("hosts", {}) if isinstance(data, dict) else {}
        accounts = hosts.get("github.com", []) if isinstance(hosts, dict) else []
        active = next(
            (
                item
                for item in accounts
                if isinstance(item, dict) and item.get("active") and item.get("state") == "success"
            ),
            None,
        )
        if not active:
            return self._status(version, False, detail=self.login_hint)
        return self._status(version, True, account=active.get("login") or active.get("username"))

    def plan(self, project: Any, environment: str) -> list[CommandSpec]:
        raise UnsupportedOperation("GitHub Actions is a CI integration, not a direct hosting target")

    def ci_status(self, project: Any) -> list[CommandSpec]:
        return [
            CommandSpec(
                [
                    self.binary,
                    "run",
                    "list",
                    "--limit",
                    "10",
                    "--json",
                    "databaseId,status,conclusion,workflowName,headBranch,url",
                ],
                "inspect recent GitHub Actions runs",
                False,
                cwd=root_of(project),
            )
        ]

    def ci_diagnose(self, project: Any, run_id: str) -> list[CommandSpec]:
        if not _RUN_ID.fullmatch(run_id):
            raise UnsupportedOperation("GitHub Actions run ID must be numeric")
        root = root_of(project)
        return [
            CommandSpec(
                [self.binary, "run", "view", run_id, "--json", "status,conclusion,jobs,url"],
                "inspect workflow run and failed jobs",
                False,
                cwd=root,
            ),
            CommandSpec(
                [self.binary, "run", "view", run_id, "--log-failed"],
                "retrieve failed workflow steps",
                False,
                timeout=90,
                cwd=root,
            ),
        ]

    def logs(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        if not release:
            return self.ci_status(project)
        return self.ci_diagnose(project, release)

    def rollback(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        raise UnsupportedOperation("GitHub Actions has no generic production rollback")
