"""Cloudflare Workers and Pages operations via Wrangler v3/v4."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import replace
from pathlib import Path
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
    require_static,
    root_of,
    value,
)


class Provider(BaseProvider):
    name = "cloudflare"
    binary = "wrangler"
    capabilities = ("workers_deploy", "pages_deploy", "deployment_logs", "workers_rollback")
    login_hint = "wrangler login"

    def detect(self, runner: Runner) -> ProviderStatus:
        version = self._version(runner)
        if version is None:
            return self._status(None, False, detail="Wrangler is unavailable")
        result = self._read(runner, "whoami", "--json")
        if not result.ok:
            return self._status(version, False, detail=self.login_hint)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return self._status(version, False, detail="Wrangler identity response could not be parsed")
        if isinstance(payload, dict) and payload.get("loggedIn") is False:
            return self._status(version, False, detail=self.login_hint)
        accounts = payload.get("accounts", []) if isinstance(payload, dict) else []
        if not isinstance(accounts, list) or not accounts:
            return self._status(version, False, detail="Wrangler identity contains no account")
        account = None
        if isinstance(accounts, list) and len(accounts) == 1 and isinstance(accounts[0], dict):
            account = str(accounts[0].get("id") or accounts[0].get("name") or "") or None
        detail = "multiple accounts; use an account-scoped Wrangler configuration" if len(accounts) > 1 else None
        return self._status(version, True, account=account, detail=detail)

    @staticmethod
    def _config(root: Path) -> dict[str, Any]:
        for name in ("wrangler.toml", "wrangler.json", "wrangler.jsonc"):
            path = root / name
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > 1024 * 1024:
                    raise UnsupportedOperation(f"{name} exceeds 1 MiB inspection limit")
                raw = path.read_text(encoding="utf-8")
                data = (
                    tomllib.loads(raw)
                    if name.endswith(".toml")
                    else json.loads(_strip_jsonc(raw) if name.endswith(".jsonc") else raw)
                )
            except (OSError, ValueError) as exc:
                raise UnsupportedOperation(f"invalid {name}: {exc}") from exc
            if isinstance(data, dict):
                return data
        raise UnsupportedOperation("a Wrangler project configuration is required")

    def plan(self, project: Any, environment: str) -> list[CommandSpec]:
        environment_name(environment)
        root = root_of(project)
        config = self._config(root)
        name = config.get("name")
        if not isinstance(name, str) or not name:
            raise UnsupportedOperation("Wrangler project name is required")
        is_pages = "pages_build_output_dir" in config
        if is_pages:
            require_static(project, self.name)
            output = output_of(project)
            configured_output = config.get("pages_build_output_dir")
            if configured_output != output:
                raise UnsupportedOperation("project output directory differs from pages_build_output_dir")
            branch = value(project, "production_branch" if environment == "production" else "preview_branch")
            if not isinstance(branch, str) or not branch:
                if environment == "production":
                    raise UnsupportedOperation(
                        "Pages production requires an explicit production_branch matching the remote project"
                    )
                branch = "devops-preview" if environment == "preview" else "devops-staging"
            argv = [
                self.binary,
                "pages",
                "deploy",
                output,
                "--project-name",
                name,
                "--branch",
                branch,
            ]
            return [
                CommandSpec(
                    argv,
                    "deploy Cloudflare Pages assets",
                    True,
                    environment == "production",
                    600,
                    root,
                )
            ]
        if not config.get("main") and not config.get("assets"):
            raise UnsupportedOperation("Wrangler Worker config needs main or assets")
        env_config = config.get("env", {})
        if environment != "production":
            if not isinstance(env_config, dict) or environment not in env_config:
                raise UnsupportedOperation(f"Wrangler environment {environment!r} is not configured")
            argv = [self.binary, "deploy", "--env", environment, "--strict"]
        else:
            argv = [self.binary, "deploy"]
            if isinstance(env_config, dict) and "production" in env_config:
                argv.extend(("--env", "production"))
            argv.append("--strict")
        return [CommandSpec(argv, "deploy Cloudflare Worker", True, environment == "production", 600, root)]

    def logs(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        root = root_of(project)
        config = self._config(root)
        if "pages_build_output_dir" in config:
            argv = [
                self.binary,
                "pages",
                "deployment",
                "list",
                "--project-name",
                config["name"],
                "--json",
            ]
            return [CommandSpec(argv, "list Pages deployments and diagnostic status", False, cwd=root)]
        if release:
            argv = [self.binary, "tail", "--version-id", release, "--format", "json"]
            return [CommandSpec(argv, "stream Worker version errors", False, timeout=15, cwd=root)]
        return [
            CommandSpec(
                [self.binary, "deployments", "list", "--json"],
                "list Worker deployments",
                False,
                cwd=root,
            )
        ]

    def rollback(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        root = root_of(project)
        config = self._config(root)
        if "pages_build_output_dir" in config:
            raise UnsupportedOperation("Wrangler Pages rollback is not exposed by this adapter")
        if not release:
            raise UnsupportedOperation("a known-good Worker version ID is required for rollback")
        if not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            release,
        ):
            raise UnsupportedOperation("Worker version ID must be a UUID")
        return [
            CommandSpec(
                [self.binary, "versions", "deploy", release, "--yes"],
                "restore known-good Worker version",
                True,
                True,
                300,
                root,
            )
        ]

    def parse_deployment(self, result: OperationResult) -> OperationResult:
        if not result.ok:
            return result
        url_match = re.search(r"https://[a-z0-9.-]+\.(?:workers|pages)\.dev(?:/[^\s]*)?", result.stdout, re.I)
        version_match = re.search(r"(?:Version ID|Deployment ID):?\s*([0-9a-fA-F-]{36})", result.stdout)
        return replace(
            result,
            url=url_match.group(0) if url_match else None,
            release=version_match.group(1) if version_match else None,
        )


def _strip_jsonc(raw: str) -> str:
    """Remove JSONC comments and trailing commas outside string literals."""
    chars: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(raw):
        char = raw[index]
        nxt = raw[index + 1] if index + 1 < len(raw) else ""
        if in_string:
            chars.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            chars.append(char)
            index += 1
            continue
        if char == "/" and nxt in {"/", "*"}:
            if nxt == "/":
                index = raw.find("\n", index + 2)
                if index == -1:
                    break
                chars.append("\n")
                index += 1
                continue
            end = raw.find("*/", index + 2)
            if end == -1:
                raise ValueError("unclosed JSONC block comment")
            chars.append(" ")
            index = end + 2
            continue
        chars.append(char)
        index += 1
    cleaned = "".join(chars)
    result: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(cleaned):
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        if char == ",":
            look = index + 1
            while look < len(cleaned) and cleaned[look].isspace():
                look += 1
            if look < len(cleaned) and cleaned[look] in "]}":
                continue
        result.append(char)
    return "".join(result)
