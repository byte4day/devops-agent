"""Vercel CLI operations for an existing linked project."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from .base import (
    BaseProvider,
    CommandSpec,
    OperationResult,
    ProviderStatus,
    Runner,
    UnsupportedOperation,
    environment_name,
    root_of,
)

_RELEASE = re.compile(r"^(?:dpl_[A-Za-z0-9]+|https://[A-Za-z0-9.-]+\.vercel\.app)$")


class Provider(BaseProvider):
    name = "vercel"
    binary = "vercel"
    capabilities = ("preview_deploy", "production_deploy", "deployment_logs", "production_rollback")
    login_hint = "vercel login"

    def detect(self, runner: Runner) -> ProviderStatus:
        version = self._version(runner)
        if version is None:
            return self._status(None, False, detail="Vercel CLI is unavailable")
        result = self._read(runner, "whoami")
        if not result.ok:
            return self._status(version, False, detail=self.login_hint)
        account = result.stdout.strip().splitlines()[-1].strip() or None
        return self._status(version, True, account=account)

    @staticmethod
    def _linked(project: Any) -> None:
        path = root_of(project) / ".vercel" / "project.json"
        if not path.is_file():
            raise UnsupportedOperation("Vercel project is not linked; run vercel link interactively")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UnsupportedOperation(f"invalid Vercel project link: {exc}") from exc
        if not isinstance(data, dict) or not data.get("projectId") or not data.get("orgId"):
            raise UnsupportedOperation("Vercel project link lacks projectId or orgId")

    def plan(self, project: Any, environment: str) -> list[CommandSpec]:
        environment_name(environment)
        root = root_of(project)
        self._linked(project)
        argv = [self.binary, "deploy", "--yes", "--logs"]
        if environment == "production":
            argv.append("--prod")
        elif environment == "staging":
            raise UnsupportedOperation("Vercel custom staging target is not configured by this adapter")
        return [CommandSpec(argv, "deploy linked Vercel project", True, environment == "production", 900, root)]

    def logs(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        root = root_of(project)
        self._linked(project)
        if release:
            if not _RELEASE.fullmatch(release):
                raise UnsupportedOperation("Vercel release must be a deployment ID or vercel.app URL")
            return [
                CommandSpec(
                    [self.binary, "inspect", release, "--logs"],
                    "inspect deployment build logs",
                    False,
                    cwd=root,
                )
            ]
        return [CommandSpec([self.binary, "logs", "--json"], "inspect recent runtime logs", False, cwd=root)]

    def rollback(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        root = root_of(project)
        self._linked(project)
        if not release or not _RELEASE.fullmatch(release):
            raise UnsupportedOperation("known-good Vercel deployment ID or URL is required")
        return [
            CommandSpec(
                [self.binary, "rollback", release],
                "restore known-good production deployment",
                True,
                True,
                300,
                root,
            )
        ]

    def parse_deployment(self, result: OperationResult) -> OperationResult:
        if not result.ok:
            return result
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        url = next(
            (line for line in reversed(lines) if re.fullmatch(r"https://[A-Za-z0-9.-]+", line)),
            None,
        )
        return replace(result, url=url)
