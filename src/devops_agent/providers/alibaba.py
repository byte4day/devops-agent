"""Alibaba CLI identity checks and explicit OSS static uploads via ossutil."""

from __future__ import annotations

import json
import re
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

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_PREFIX = re.compile(r"^(?:[A-Za-z0-9._/-]*/)?$")
_INSTANCE = re.compile(r"^i-[A-Za-z0-9]{6,64}$")
_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+$")


class Provider(BaseProvider):
    name = "alibaba"
    binary = "aliyun"
    version_args = ("version",)
    capabilities = ("identity_inspection", "oss_static_upload", "ecs_instance_inspection")
    login_hint = "aliyun configure --mode OAuth"

    def detect(self, runner: Runner) -> ProviderStatus:
        version = self._version(runner)
        if version is None:
            return self._status(None, False, detail="Alibaba Cloud CLI is unavailable")
        # Legacy OpenAPI form remains supported and avoids auto-installing a
        # product plugin during read-only discovery on CLI 3.3+.
        result = self._read(runner, "sts", "GetCallerIdentity")
        region_result = self._read(runner, "configure", "get", "region")
        region = region_result.stdout.strip() if region_result.ok else None
        if not result.ok:
            return self._status(version, False, region=region, detail=self.login_hint)
        try:
            account = str(json.loads(result.stdout)["AccountId"])
        except (ValueError, KeyError, TypeError):
            return self._status(version, False, region=region, detail="STS identity response could not be parsed")
        oss_spec = CommandSpec(["ossutil", "version"], "inspect ossutil version", False, timeout=15)
        try:
            oss = runner.run(oss_spec)
        except (FileNotFoundError, OSError):
            oss = OperationResult(oss_spec, 127)
        capabilities = self.capabilities if oss.ok else ("identity_inspection", "ecs_instance_inspection")
        detail = None if oss.ok else "ossutil unavailable; OSS deployment unsupported"
        return self._status(version, True, account=account, region=region, detail=detail, capabilities=capabilities)

    @staticmethod
    def _destination(project: Any) -> str:
        bucket = value(project, "oss_bucket")
        prefix = value(project, "oss_prefix", "")
        if not isinstance(bucket, str) or not _BUCKET.fullmatch(bucket):
            raise UnsupportedOperation("OSS deployment requires an explicit valid oss_bucket")
        if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix) or ".." in prefix.split("/"):
            raise UnsupportedOperation("OSS prefix must be a safe relative key prefix")
        return f"oss://{bucket}/{prefix.lstrip('/')}"

    def plan(self, project: Any, environment: str) -> list[CommandSpec]:
        environment_name(environment)
        require_static(project, self.name)
        root = root_of(project)
        output = output_of(project)
        destination = self._destination(project)
        if environment != "production" and not value(project, "oss_prefix"):
            raise UnsupportedOperation("preview/staging OSS uploads require an isolated oss_prefix")
        source = str(root / output) + "/"
        return [
            CommandSpec(
                ["ossutil", "cp", "-r", source, destination],
                "upload static files to existing OSS bucket",
                True,
                environment == "production",
                900,
                root,
            )
        ]

    def preflight(self, project: Any) -> list[CommandSpec]:
        root = root_of(project)
        destination = self._destination(project)
        return [
            CommandSpec(
                ["ossutil", "ls", destination, "--limited-num=50"],
                "verify OSS bucket and prefix access",
                False,
                cwd=root,
            )
        ]

    def logs(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        root = root_of(project)
        return [
            CommandSpec(
                ["ossutil", "ls", self._destination(project), "--limited-num=50"],
                "inspect OSS uploaded objects",
                False,
                cwd=root,
            )
        ]

    def diagnose_ecs(self, project: Any, instance_id: str, region: str) -> list[CommandSpec]:
        """Inspect one known ECS instance without changing it or enumerating the account."""
        if not _INSTANCE.fullmatch(instance_id):
            raise UnsupportedOperation("a valid ECS instance ID is required")
        if not _REGION.fullmatch(region):
            raise UnsupportedOperation("an explicit Alibaba Cloud region is required")
        return [
            CommandSpec(
                [
                    "aliyun",
                    "ecs",
                    "DescribeInstances",
                    "--RegionId",
                    region,
                    "--InstanceIds",
                    json.dumps([instance_id]),
                    "--output",
                    "cols=InstanceId,InstanceName,Status,InstanceType rows=Instances.Instance[]",
                ],
                f"inspect ECS instance {instance_id} in {region}",
                False,
                cwd=root_of(project),
            )
        ]

    def rollback(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        raise UnsupportedOperation("OSS static upload rollback requires separately configured object versioning")
