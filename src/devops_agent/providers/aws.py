"""AWS CLI discovery and explicitly configured CloudFormation deployment."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .base import (
    BaseProvider,
    CommandSpec,
    ProviderStatus,
    Runner,
    UnsupportedOperation,
    environment_name,
    output_of,
    require_static,
    root_of,
    value,
)

_STACK = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PREFIX = re.compile(r"^(?:[A-Za-z0-9._/-]*/)?$")


class Provider(BaseProvider):
    name = "aws"
    binary = "aws"
    capabilities = (
        "s3_static_upload",
        "s3_dry_run",
        "cloudformation_validate",
        "cloudformation_events",
    )
    login_hint = "aws configure sso; aws sso login --profile PROFILE"

    def detect(self, runner: Runner) -> ProviderStatus:
        version = self._version(runner)
        if version is None:
            return self._status(None, False, detail="AWS CLI is unavailable")
        result = self._read(runner, "sts", "get-caller-identity", "--output", "json", "--no-cli-pager")
        region_result = self._read(runner, "configure", "get", "region")
        region = (
            region_result.stdout.strip()
            if region_result.ok
            else os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        )
        profile = os.environ.get("AWS_PROFILE")
        if not result.ok:
            return self._status(version, False, region=region, detail=self.login_hint)
        try:
            identity = json.loads(result.stdout)
            account = str(identity["Account"])
        except (ValueError, KeyError, TypeError):
            return self._status(version, False, region=region, detail="STS identity response could not be parsed")
        detail = f"profile: {profile}" if profile else "default credential chain"
        return self._status(version, True, account=account, region=region, detail=detail)

    @staticmethod
    def _cloudformation_target(project: Any) -> tuple[str, str, str | None]:
        root = root_of(project)
        template = value(project, "cloudformation_template")
        stack = value(project, "cloudformation_stack")
        region = value(project, "aws_region")
        if not isinstance(template, str) or not template or not isinstance(stack, str) or not _STACK.fullmatch(stack):
            raise UnsupportedOperation(
                "AWS deployment requires cloudformation_template and a valid cloudformation_stack"
            )
        template_path = (root / template).resolve()
        if not template_path.is_relative_to(root) or not template_path.is_file():
            raise UnsupportedOperation("CloudFormation template must be an existing project file")
        if region is not None and (
            not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", region)
        ):
            raise UnsupportedOperation("aws_region is invalid")
        return str(template_path), stack, region

    @staticmethod
    def _s3_target(project: Any) -> tuple[str, str | None]:
        bucket = value(project, "aws_bucket")
        prefix = value(project, "aws_prefix", "")
        region = value(project, "aws_region")
        if not isinstance(bucket, str) or not _BUCKET.fullmatch(bucket) or ".." in bucket:
            raise UnsupportedOperation("AWS static deployment requires an explicit valid aws_bucket")
        if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix) or ".." in prefix.split("/"):
            raise UnsupportedOperation("aws_prefix must be a safe relative key prefix")
        if region is not None and (
            not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", region)
        ):
            raise UnsupportedOperation("aws_region is invalid")
        owner = value(project, "aws_account_id")
        if not isinstance(owner, str) or not re.fullmatch(r"[0-9]{12}", owner):
            raise UnsupportedOperation("AWS deployment requires a 12-digit aws_account_id for bucket owner scoping")
        return f"s3://{bucket}/{prefix.lstrip('/')}", region

    def plan(self, project: Any, environment: str) -> list[CommandSpec]:
        environment_name(environment)
        require_static(project, self.name)
        root = root_of(project)
        destination, region = self._s3_target(project)
        if environment != "production" and not value(project, "aws_prefix"):
            raise UnsupportedOperation("preview/staging S3 uploads require an isolated aws_prefix")
        source = str(root / output_of(project)) + "/"
        argv = [
            self.binary,
            "s3",
            "sync",
            source,
            destination,
            "--no-follow-symlinks",
            "--only-show-errors",
            "--no-cli-pager",
        ]
        if region:
            argv.extend(("--region", region))
        return [
            CommandSpec(
                argv,
                "upload static files to existing S3 bucket without deleting objects",
                True,
                environment == "production",
                900,
                root,
            )
        ]

    def preflight(self, project: Any) -> list[CommandSpec]:
        root = root_of(project)
        destination, region = self._s3_target(project)
        bucket = destination.removeprefix("s3://").split("/", 1)[0]
        owner = value(project, "aws_account_id")
        argv = [self.binary, "s3api", "head-bucket", "--bucket", bucket, "--no-cli-pager"]
        argv.extend(("--expected-bucket-owner", owner))
        if region:
            argv.extend(("--region", region))
        return [CommandSpec(argv, "verify S3 bucket and expected account owner", False, cwd=root)]

    def dry_run(self, project: Any, environment: str) -> list[CommandSpec]:
        """Preview upload changes after the build output exists."""
        sync = self.plan(project, environment)[0]
        return [
            CommandSpec(
                [*sync.argv, "--dryrun"],
                "preview S3 object changes",
                False,
                timeout=300,
                cwd=sync.cwd,
            )
        ]

    def validate_cloudformation(self, project: Any) -> list[CommandSpec]:
        root = root_of(project)
        template, _, region = self._cloudformation_target(project)
        argv = [
            self.binary,
            "cloudformation",
            "validate-template",
            "--template-body",
            f"file://{template}",
            "--no-cli-pager",
        ]
        if region:
            argv.extend(("--region", region))
        return [CommandSpec(argv, "validate CloudFormation template without deploying", False, cwd=root)]

    def logs(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        root = root_of(project)
        try:
            _, stack, region = self._cloudformation_target(project)
        except UnsupportedOperation:
            destination, region = self._s3_target(project)
            bucket = destination.removeprefix("s3://").split("/", 1)[0]
            argv = [
                self.binary,
                "s3api",
                "list-objects-v2",
                "--bucket",
                bucket,
                "--max-items",
                "20",
                "--output",
                "json",
                "--no-cli-pager",
            ]
            if region:
                argv.extend(("--region", region))
            return [CommandSpec(argv, "inspect recent S3 object inventory", False, cwd=root)]
        argv = [
            self.binary,
            "cloudformation",
            "describe-stack-events",
            "--stack-name",
            stack,
            "--max-items",
            "20",
            "--output",
            "json",
            "--no-cli-pager",
        ]
        if region:
            argv.extend(("--region", region))
        return [CommandSpec(argv, "inspect recent CloudFormation stack events", False, cwd=root)]

    def rollback(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        raise UnsupportedOperation("manual CloudFormation rollback requires a reviewed prior template and parameters")
