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
_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


class Provider(BaseProvider):
    name = "vercel"
    binary = "vercel"
    capabilities = (
        "preview_deploy",
        "production_deploy",
        "deployment_logs",
        "production_rollback",
        "project_inspection",
        "domain_inspection",
        "environment_inspection",
        "additive_domain_plan",
    )
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
    def _linked(project: Any) -> dict[str, str]:
        path = root_of(project) / ".vercel" / "project.json"
        if not path.is_file():
            raise UnsupportedOperation("Vercel project is not linked; run vercel link interactively")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UnsupportedOperation(f"invalid Vercel project link: {exc}") from exc
        if not isinstance(data, dict) or not data.get("projectId") or not data.get("orgId"):
            raise UnsupportedOperation("Vercel project link lacks projectId or orgId")
        return {"projectId": str(data["projectId"]), "orgId": str(data["orgId"])}

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

    def inspect_project(self, project: Any) -> list[CommandSpec]:
        root = root_of(project)
        self._linked(project)
        return [
            CommandSpec(
                [self.binary, "project", "inspect"],
                "inspect linked Vercel project",
                False,
                cwd=root,
            )
        ]

    def inspect_domains(self, project: Any) -> list[CommandSpec]:
        root = root_of(project)
        self._linked(project)
        return [
            CommandSpec(
                [self.binary, "domains", "ls"],
                "list visible Vercel domains; ownership must be checked per hostname",
                False,
                cwd=root,
            )
        ]

    def inspect_environment(self, project: Any, environment: str) -> list[CommandSpec]:
        environment_name(environment)
        root = root_of(project)
        self._linked(project)
        if environment == "staging":
            raise UnsupportedOperation("custom Vercel staging target is not configured by this adapter")
        argv = (
            [self.binary, "list", "--prod"]
            if environment == "production"
            else [
                self.binary,
                "list",
                "--environment=preview",
            ]
        )
        return [CommandSpec(argv, f"inspect Vercel {environment} deployments", False, cwd=root)]

    def plan_domain_change(self, project: Any, hostname: str, target: str) -> dict[str, object]:
        root = root_of(project)
        linked = self._linked(project)
        hostname = hostname.strip().lower().rstrip(".")
        if not _valid_hostname(hostname):
            raise UnsupportedOperation("domain must be a valid ASCII hostname, without a wildcard")
        if not _PROJECT_NAME.fullmatch(target):
            raise UnsupportedOperation("Vercel target must be a valid project name")
        inspection = [
            CommandSpec(
                [self.binary, "project", "inspect", target],
                "confirm target project and organization before mutation",
                False,
                cwd=root,
            ),
            CommandSpec(
                [self.binary, "domains", "inspect", hostname],
                "confirm domain ownership and current assignment before mutation",
                False,
                cwd=root,
            ),
        ]
        mutation = [
            CommandSpec(
                [self.binary, "domains", "add", hostname, target],
                "add an unassigned domain to the verified Vercel project",
                True,
                True,
                300,
                root,
            )
        ]
        return {
            "inspection": inspection,
            "mutation": mutation,
            "hostname": hostname,
            "target": target,
            "approval_required": True,
            "expected_project_id": linked["projectId"],
            "expected_org_id": linked["orgId"],
            "preconditions": [
                "Remote project inspection must match both linked projectId and orgId.",
                "Domain inspection must establish ownership in the same scope and no existing project assignment.",
                "Re-run inspection immediately before applying; reject missing, ambiguous, or conflicting evidence.",
                "Require approval for the exact hostname and target; never use --force or change DNS implicitly.",
            ],
        }

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


def _valid_hostname(hostname: str) -> bool:
    if len(hostname) > 253 or "." not in hostname:
        return False
    labels = hostname.split(".")
    return all(
        1 <= len(label) <= 63 and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is not None
        for label in labels
    )
