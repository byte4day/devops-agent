"""Provider selection and immutable deployment planning."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from devops_agent.core.analyzer import ProjectAnalysis
from devops_agent.core.approvals import ApprovalScope
from devops_agent.core.config import Config
from devops_agent.core.security import redact
from devops_agent.providers.base import CommandSpec, ProviderStatus, UnsupportedOperation


@dataclass(frozen=True)
class DeploymentPlan:
    project: str
    provider: str
    environment: str
    account: str | None
    preflight: tuple[CommandSpec, ...]
    build: tuple[CommandSpec, ...]
    deploy: tuple[CommandSpec, ...]
    health_urls: tuple[str, ...]
    application_health_required: bool
    warnings: tuple[str, ...]
    artifact_output_dir: str | None = None
    approval_target: str | None = None

    @property
    def approval_phrase(self) -> str | None:
        if self.environment != "production" and self.provider != "aws":
            return None
        return ApprovalScope("deploy", self.provider, self.approval_target or self.project, self.environment).phrase

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        for group in ("preflight", "build", "deploy"):
            data[group] = [
                {**asdict(spec), "argv": [redact(arg) for arg in spec.argv], "cwd": str(spec.cwd) if spec.cwd else None}
                for spec in getattr(self, group)
            ]
        data["approval_phrase"] = self.approval_phrase
        return data


def choose_provider(
    project: ProjectAnalysis, config: Config, statuses: list[ProviderStatus], requested: str | None
) -> str:
    by_name = {status.name: status for status in statuses}
    explicit = requested or config.provider
    if explicit:
        if explicit not in by_name:
            raise ValueError(f"Unknown provider {explicit}")
        return explicit
    configured = (
        ("wrangler.toml", "cloudflare"),
        ("wrangler.json", "cloudflare"),
        ("wrangler.jsonc", "cloudflare"),
        (".vercel/project.json", "vercel"),
        ("vercel.json", "vercel"),
        ("netlify.toml", "netlify"),
        ("amplify.yml", "aws"),
    )
    matches = {provider for marker, provider in configured if marker in project.hosting_configs}
    if len(matches) == 1:
        return next(iter(matches))
    if len(matches) > 1:
        raise ValueError("Multiple hosting providers are configured; select --provider explicitly")
    compatible = [
        s.name for s in statuses if s.installed and s.authenticated and s.name in {"cloudflare", "vercel", "netlify"}
    ]
    if len(compatible) == 1:
        return compatible[0]
    raise ValueError("No unambiguous existing provider; pass --provider after reviewing compatibility and cost")


def make_plan(
    project: ProjectAnalysis,
    config: Config,
    provider: object,
    status: ProviderStatus,
    environment: str,
) -> DeploymentPlan:
    if environment not in config.allowed_targets:
        raise PermissionError(f"Target {environment} is not in allowed_targets")
    if project.services and len(project.services) > 1:
        raise ValueError("Multiple services detected; run from the deployable service directory")
    if not status.installed:
        raise RuntimeError(f"{status.name} CLI is unavailable")
    if not status.authenticated:
        raise RuntimeError(f"{status.name} is not authenticated: {status.detail or 'run the official login command'}")
    if status.name == "cloudflare" and not status.account:
        raise UnsupportedOperation(
            "Cloudflare has multiple or unknown accounts; select one with scoped Wrangler configuration"
        )
    build: list[CommandSpec] = []
    test_commands = config.test_commands or project.test_commands
    build_commands = config.build_commands or ((project.build_command,) if project.build_command else ())
    for index, command in enumerate(test_commands):
        build.append(
            CommandSpec(
                list(command),
                f"test {index + 1}",
                False,
                timeout=config.command_timeout,
                cwd=project.root,
            )
        )
    for index, command in enumerate(build_commands):
        build.append(
            CommandSpec(
                list(command),
                f"build {index + 1}",
                False,
                timeout=config.command_timeout,
                cwd=project.root,
            )
        )
    target = provider_target(project, config)
    preflight = tuple(provider.preflight(target)) if hasattr(provider, "preflight") else ()
    if any(step.mutating for step in preflight):
        raise UnsupportedOperation("Provider preflight cannot mutate resources")
    deploy = tuple(provider.plan(target, environment))
    if not deploy or any(not step.mutating for step in deploy):
        raise UnsupportedOperation("Provider returned no executable deployment steps")
    artifact_output_dir = None
    if project.static and (
        provider.name in {"aws", "alibaba"}
        or provider.name == "netlify"
        and "--dir" in deploy[0].argv
        or provider.name == "cloudflare"
        and deploy[0].argv[1:3] == ["pages", "deploy"]
    ):
        artifact_output_dir = project.output_dir
    warnings = ["Production requires target-bound approval"] if environment == "production" else []
    app_health_required = project.api_routes or bool(project.database_dependencies)
    if app_health_required and not config.health_urls:
        warnings.append("API or database health requires a configured application health URL")
    elif not config.health_urls:
        warnings.append("No health URL configured; release will be UNVERIFIED unless the provider returns a URL")
    if app_health_required and environment != "production":
        warnings.append("Preview application health needs a release-specific health URL")
    approval_digest = hashlib.sha256(
        json.dumps(
            {
                "account": status.account,
                "root": str(project.root),
                "provider": status.name,
                "environment": environment,
                "commands": [spec.argv for spec in deploy],
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    approval_target = f"{project.name}@{status.account or 'unknown'}#{approval_digest}"
    return DeploymentPlan(
        project.name,
        status.name,
        environment,
        status.account,
        preflight,
        tuple(build),
        deploy,
        config.health_urls,
        app_health_required,
        tuple(warnings),
        artifact_output_dir,
        approval_target,
    )


def provider_target(project: ProjectAnalysis, config: Config) -> dict[str, object]:
    target = {
        **project.__dict__,
        "aws_bucket": config.aws_bucket,
        "aws_prefix": config.aws_prefix,
        "aws_region": config.aws_region,
        "aws_account_id": config.aws_account_id,
        "cloudformation_template": config.cloudformation_template,
        "cloudformation_stack": config.cloudformation_stack,
        "oss_bucket": config.oss_bucket,
        "oss_prefix": config.oss_prefix,
        "production_branch": config.production_branch,
        "preview_branch": config.preview_branch,
    }
    return {key: value for key, value in target.items() if value is not None}
