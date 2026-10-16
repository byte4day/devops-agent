"""Netlify CLI draft and production deployment for a linked site."""

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
    output_of,
    root_of,
    value,
)

_DEPLOY_ID = re.compile(r"^[0-9a-f]{24,64}$")


class Provider(BaseProvider):
    name = "netlify"
    binary = "netlify"
    capabilities = (
        "draft_deploy",
        "production_deploy",
        "deployment_inspection",
        "production_rollback",
    )
    login_hint = "netlify login"

    def detect(self, runner: Runner) -> ProviderStatus:
        version = self._version(runner)
        if version is None:
            return self._status(None, False, detail="Netlify CLI is unavailable")
        result = self._read(runner, "api", "getCurrentUser")
        if not result.ok:
            return self._status(version, False, detail=self.login_hint)
        try:
            data = json.loads(result.stdout)
        except ValueError:
            return self._status(version, False, detail="Netlify identity response could not be parsed")
        account = data.get("email") if isinstance(data, dict) and data.get("id") else None
        if not isinstance(account, str) or not account:
            return self._status(version, False, detail="Netlify identity is missing id or email")
        return self._status(version, True, account=account)

    @staticmethod
    def _linked(project: Any) -> str:
        path = root_of(project) / ".netlify" / "state.json"
        if not path.is_file():
            raise UnsupportedOperation("Netlify site is not linked; run netlify link interactively")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UnsupportedOperation(f"invalid Netlify site link: {exc}") from exc
        site_id = data.get("siteId") if isinstance(data, dict) else None
        if not isinstance(site_id, str) or not site_id:
            raise UnsupportedOperation("Netlify site link lacks siteId")
        return site_id

    def plan(self, project: Any, environment: str) -> list[CommandSpec]:
        environment_name(environment)
        root = root_of(project)
        site_id = self._linked(project)
        output = value(project, "output_dir")
        argv = [self.binary, "deploy", "--site", site_id, "--json"]
        static = value(project, "static") is True and value(project, "api_routes") is not True
        if static and isinstance(output, str) and output:
            argv.extend(("--dir", output_of(project), "--no-build"))
        elif not static and value(project, "framework") != "Next.js":
            raise UnsupportedOperation("Netlify runtime deployment is verified only for linked Next.js projects")
        if environment == "production":
            argv.append("--prod")
        elif environment == "staging":
            argv.extend(("--alias", "devops-staging"))
        return [CommandSpec(argv, "deploy linked Netlify site", True, environment == "production", 900, root)]

    def logs(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        root = root_of(project)
        self._linked(project)
        if release:
            if not _DEPLOY_ID.fullmatch(release):
                raise UnsupportedOperation("Netlify release must be a deployment ID")
            return [
                CommandSpec(
                    [self.binary, "api", "getDeploy", "--data", json.dumps({"deploy_id": release})],
                    "inspect Netlify deployment",
                    False,
                    cwd=root,
                )
            ]
        return [CommandSpec([self.binary, "status", "--json"], "inspect linked Netlify site", False, cwd=root)]

    def rollback(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        root = root_of(project)
        site_id = self._linked(project)
        if not release or not _DEPLOY_ID.fullmatch(release):
            raise UnsupportedOperation("known-good Netlify deployment ID is required")
        data = json.dumps({"site_id": site_id, "deploy_id": release})
        return [
            CommandSpec(
                [self.binary, "api", "restoreSiteDeploy", "--data", data],
                "restore known-good Netlify deployment",
                True,
                True,
                300,
                root,
            )
        ]

    def parse_deployment(self, result: OperationResult) -> OperationResult:
        if not result.ok:
            return result
        try:
            data = json.loads(result.stdout)
        except ValueError:
            return result
        if not isinstance(data, dict):
            return result
        url = data.get("deploy_ssl_url") or data.get("deploy_url") or data.get("ssl_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            url = None
        release = data.get("id") if isinstance(data.get("id"), str) else None
        return replace(result, url=url, release=release)
