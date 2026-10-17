"""Validated optional project configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml


@dataclass(frozen=True)
class Config:
    provider: str | None = None
    project_name: str | None = None
    output_dir: str | None = None
    default_environment: str = "preview"
    build_commands: tuple[tuple[str, ...], ...] = ()
    test_commands: tuple[tuple[str, ...], ...] = ()
    health_urls: tuple[str, ...] = ()
    allowed_targets: tuple[str, ...] = ("preview", "staging", "production")
    max_repair_attempts: int = 3
    command_timeout: int = 300
    approval_policy: dict[str, bool] = field(default_factory=dict)
    aws_bucket: str | None = None
    aws_prefix: str | None = None
    aws_region: str | None = None
    aws_account_id: str | None = None
    cloudformation_template: str | None = None
    cloudformation_stack: str | None = None
    oss_bucket: str | None = None
    oss_prefix: str | None = None
    production_branch: str | None = None
    preview_branch: str | None = None


def _commands(data: object, field_name: str) -> tuple[tuple[str, ...], ...]:
    if data is None:
        return ()
    if not isinstance(data, list) or any(
        not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command)
        for command in data
    ):
        raise ValueError(f"{field_name} must be a list of nonempty argument arrays")
    sensitive = re.compile(
        r"(?i)(?:^--?(?:token|password|secret|api[-_]?key|access[-_]?key)$|"
        r"(?:secret|token|password|api[_-]?key|private[_-]?key)\s*=)"
    )
    if any(sensitive.search(arg) for command in data for arg in command):
        raise ValueError(f"{field_name} cannot contain credential arguments; use a secret store")
    return tuple(tuple(command) for command in data)


def load_config(root: Path) -> Config:
    path = root / "devops.yaml"
    if not path.exists():
        return Config()
    if path.is_symlink():
        raise ValueError("devops.yaml cannot be a symlink")
    if path.stat().st_size > 65536:
        raise ValueError("devops.yaml exceeds 64 KiB")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid devops.yaml: {getattr(exc, 'problem', None) or type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise ValueError("devops.yaml must be a mapping")
    allowed = {
        "provider",
        "project_name",
        "output_dir",
        "default_environment",
        "build_commands",
        "test_commands",
        "health_urls",
        "allowed_targets",
        "max_repair_attempts",
        "command_timeout",
        "approval_policy",
        "aws_bucket",
        "aws_prefix",
        "aws_region",
        "aws_account_id",
        "cloudformation_template",
        "cloudformation_stack",
        "oss_bucket",
        "oss_prefix",
        "production_branch",
        "preview_branch",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"Unknown devops.yaml keys: {', '.join(sorted(unknown))}")
    for key in (
        "provider",
        "project_name",
        "output_dir",
        "aws_bucket",
        "aws_prefix",
        "aws_region",
        "aws_account_id",
        "cloudformation_template",
        "cloudformation_stack",
        "oss_bucket",
        "oss_prefix",
        "production_branch",
        "preview_branch",
    ):
        if raw.get(key) is not None and (not isinstance(raw[key], str) or not raw[key].strip()):
            raise ValueError(f"{key} must be a nonempty string")
    environment = raw.get("default_environment", "preview")
    output_dir = raw.get("output_dir")
    if output_dir is not None and (
        output_dir in {".", "./"} or Path(output_dir).is_absolute() or ".." in Path(output_dir).parts
    ):
        raise ValueError("output_dir must be a safe relative directory, not the project root")
    if environment not in {"preview", "staging", "production"}:
        raise ValueError("default_environment must be preview, staging, or production")
    targets = raw.get("allowed_targets", ["preview", "staging", "production"])
    if (
        not isinstance(targets, list)
        or not targets
        or any(t not in {"preview", "staging", "production"} for t in targets)
    ):
        raise ValueError("allowed_targets must be a nonempty list of valid environments")
    urls = raw.get("health_urls", [])
    if not isinstance(urls, list) or any(
        not isinstance(u, str) or urlparse(u).scheme not in {"http", "https"} or not urlparse(u).hostname for u in urls
    ):
        raise ValueError("health_urls must contain absolute HTTP(S) URLs")
    retries = raw.get("max_repair_attempts", 3)
    timeout = raw.get("command_timeout", 300)
    if type(retries) is not int or retries < 0 or retries > 3:
        raise ValueError("max_repair_attempts must be an integer from 0 to 3")
    if type(timeout) is not int or timeout < 1 or timeout > 1800:
        raise ValueError("command_timeout must be an integer from 1 to 1800")
    approval = raw.get("approval_policy", {})
    if (
        not isinstance(approval, dict)
        or set(approval) - {"require_production_approval"}
        or any(type(v) is not bool for v in approval.values())
    ):
        raise ValueError("approval_policy supports only require_production_approval: boolean")
    if approval.get("require_production_approval") is False:
        raise ValueError("devops.yaml cannot waive production approval")
    return Config(
        provider=raw.get("provider"),
        project_name=raw.get("project_name"),
        output_dir=output_dir,
        default_environment=environment,
        build_commands=_commands(raw.get("build_commands"), "build_commands"),
        test_commands=_commands(raw.get("test_commands"), "test_commands"),
        health_urls=tuple(urls),
        allowed_targets=tuple(targets),
        max_repair_attempts=retries,
        command_timeout=timeout,
        approval_policy=approval,
        aws_bucket=raw.get("aws_bucket"),
        aws_prefix=raw.get("aws_prefix"),
        aws_region=raw.get("aws_region"),
        aws_account_id=raw.get("aws_account_id"),
        cloudformation_template=raw.get("cloudformation_template"),
        cloudformation_stack=raw.get("cloudformation_stack"),
        oss_bucket=raw.get("oss_bucket"),
        oss_prefix=raw.get("oss_prefix"),
        production_branch=raw.get("production_branch"),
        preview_branch=raw.get("preview_branch"),
    )
