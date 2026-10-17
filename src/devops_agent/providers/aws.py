"""AWS CLI discovery, scoped S3 uploads, and reviewed CloudFormation updates."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from hashlib import sha256
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
_ACCOUNT = re.compile(r"^[0-9]{12}$")
_PARAMETER = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,254}$")
_CHANGESET_QUERY = (
    "{ChangeSetId:ChangeSetId,StackId:StackId,StackName:StackName,"
    "Status:Status,ExecutionStatus:ExecutionStatus,"
    "Capabilities:Capabilities,IncludeNestedStacks:IncludeNestedStacks,"
    "ImportExistingResources:ImportExistingResources,"
    "Changes:Changes[].ResourceChange.{Action:Action,LogicalResourceId:LogicalResourceId,"
    "ResourceType:ResourceType,Replacement:Replacement}}"
)
_STACK_QUERY = "Stacks[0].{StackId:StackId,StackStatus:StackStatus,ParameterKeys:Parameters[].ParameterKey}"


@dataclass(frozen=True)
class ChangeSetReview:
    """Secret-minimized, target-bound summary to show before explicit execution."""

    change_set_arn: str
    stack_arn: str
    stack_name: str
    account: str
    region: str
    status: str
    execution_status: str
    changes: tuple[tuple[str, str, str, str], ...]
    digest: str
    destructive: bool
    capabilities: tuple[str, ...] = ()
    include_nested_stacks: bool = False
    import_existing_resources: bool = False


def _arn_parts(arn: str, resource_type: str) -> tuple[str, str, str, str]:
    if not isinstance(arn, str):
        raise UnsupportedOperation("CloudFormation ARN is invalid")
    parts = arn.split(":", 5)
    if (
        len(parts) != 6
        or parts[0] != "arn"
        or parts[1] not in {"aws", "aws-cn", "aws-us-gov"}
        or parts[2] != "cloudformation"
        or not parts[3]
        or not _ACCOUNT.fullmatch(parts[4])
    ):
        raise UnsupportedOperation("CloudFormation ARN has an unexpected account or region")
    resource = parts[5].split("/")
    if len(resource) != 3 or resource[0] != resource_type or not all(resource[1:]):
        raise UnsupportedOperation("CloudFormation ARN has an unexpected resource type")
    return parts[1], parts[3], parts[4], resource[1]


class Provider(BaseProvider):
    name = "aws"
    binary = "aws"
    capabilities = (
        "s3_static_upload",
        "s3_dry_run",
        "cloudformation_validate",
        "cloudformation_events",
        "cloudformation_reviewed_update",
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

    @staticmethod
    def _expected_account(project: Any) -> str:
        account = value(project, "aws_account_id")
        if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
            raise UnsupportedOperation("CloudFormation update requires a 12-digit aws_account_id")
        return account

    def _checked_identity(self, project: Any, runner: Runner, region: str) -> None:
        result = self._read(
            runner,
            "sts",
            "get-caller-identity",
            "--query",
            "Account",
            "--output",
            "text",
            "--region",
            region,
            "--no-cli-pager",
        )
        if not result.ok or result.stdout.strip() != self._expected_account(project):
            raise UnsupportedOperation("active AWS identity does not match configured aws_account_id")

    def _cf_target(self, project: Any) -> tuple[str, str, str, str]:
        template, stack, region = self._cloudformation_target(project)
        if region is None:
            raise UnsupportedOperation("CloudFormation updates require an explicit aws_region")
        if os.path.getsize(template) > 51_200:
            raise UnsupportedOperation("CloudFormation template exceeds the CLI inline-body limit (51,200 bytes)")
        return template, stack, region, self._expected_account(project)

    def cloudformation_prepare(self, project: Any, runner: Runner, change_set_name: str) -> list[CommandSpec]:
        """Prepare an UPDATE change set; creation is a guarded control-plane mutation."""
        template, stack, region, account = self._cf_target(project)
        if not _STACK.fullmatch(change_set_name):
            raise UnsupportedOperation("change set name must be alphanumeric with safe hyphens")
        self._checked_identity(project, runner, region)
        root = root_of(project)
        inspect = CommandSpec(
            [
                "aws",
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                stack,
                "--query",
                _STACK_QUERY,
                "--output",
                "json",
                "--region",
                region,
                "--no-cli-pager",
            ],
            "inspect existing CloudFormation stack without parameter values",
            False,
            cwd=root,
        )
        result = runner.run(inspect)
        if not result.ok:
            raise UnsupportedOperation("existing CloudFormation stack could not be inspected")
        try:
            data = json.loads(result.stdout)
            stack_arn = data["StackId"]
            status = data["StackStatus"]
            keys = data.get("ParameterKeys") or []
        except (ValueError, KeyError, TypeError) as exc:
            raise UnsupportedOperation("CloudFormation stack response is invalid") from exc
        _, observed_region, observed_account, observed_stack = _arn_parts(stack_arn, "stack")
        if (observed_region, observed_account, observed_stack) != (region, account, stack):
            raise UnsupportedOperation(
                "CloudFormation stack target does not match configured account, region, and name"
            )
        if status not in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}:
            raise UnsupportedOperation(f"CloudFormation stack is not ready for an update: {status}")
        if not isinstance(keys, list) or any(not isinstance(k, str) or not _PARAMETER.fullmatch(k) for k in keys):
            raise UnsupportedOperation("CloudFormation parameter keys could not be validated")
        argv = [
            "aws",
            "cloudformation",
            "create-change-set",
            "--stack-name",
            stack,
            "--change-set-name",
            change_set_name,
            "--template-body",
            f"file://{template}",
            "--change-set-type",
            "UPDATE",
            "--region",
            region,
            "--output",
            "json",
            "--no-cli-pager",
        ]
        if keys:
            argv.extend(
                (
                    "--parameters",
                    json.dumps(
                        [{"ParameterKey": key, "UsePreviousValue": True} for key in keys],
                        separators=(",", ":"),
                    ),
                )
            )
        return [
            CommandSpec(
                argv, f"prepare CloudFormation change set for {stack} in {account}/{region}", True, True, 120, root
            ),
            CommandSpec(
                [
                    "aws",
                    "cloudformation",
                    "wait",
                    "change-set-create-complete",
                    "--stack-name",
                    stack,
                    "--change-set-name",
                    change_set_name,
                    "--region",
                    region,
                    "--no-cli-pager",
                ],
                "wait for CloudFormation change set preparation",
                False,
                timeout=900,
                cwd=root,
            ),
        ]

    def cloudformation_review(self, project: Any, runner: Runner, change_set_arn: str) -> ChangeSetReview:
        """Fetch a fresh, value-free change summary and bind it to the target."""
        _, stack, region, account = self._cf_target(project)
        _, arn_region, arn_account, _ = _arn_parts(change_set_arn, "changeSet")
        if (arn_region, arn_account) != (region, account):
            raise UnsupportedOperation("change set ARN targets a different account or region")
        self._checked_identity(project, runner, region)
        spec = CommandSpec(
            [
                "aws",
                "cloudformation",
                "describe-change-set",
                "--change-set-name",
                change_set_arn,
                "--stack-name",
                stack,
                "--no-include-property-values",
                "--query",
                _CHANGESET_QUERY,
                "--output",
                "json",
                "--region",
                region,
                "--no-cli-pager",
            ],
            "review CloudFormation resource actions without parameter values",
            False,
            cwd=root_of(project),
        )
        result = runner.run(spec)
        if not result.ok:
            raise UnsupportedOperation("CloudFormation change set could not be inspected")
        try:
            data = json.loads(result.stdout)
            observed_id = data["ChangeSetId"]
            stack_arn = data["StackId"]
            observed_stack = data["StackName"]
            status = data["Status"]
            execution_status = data["ExecutionStatus"]
            capabilities = tuple(data.get("Capabilities") or ())
            nested = data.get("IncludeNestedStacks") is True
            imports = data.get("ImportExistingResources") is True
            raw_changes = data["Changes"]
            changes = tuple(
                (
                    c["Action"],
                    c["LogicalResourceId"],
                    c["ResourceType"],
                    str(c.get("Replacement") or ("Unknown" if c["Action"] == "Modify" else "False")),
                )
                for c in raw_changes
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise UnsupportedOperation("CloudFormation change set summary is invalid") from exc
        _, stack_region, stack_account, stack_in_arn = _arn_parts(stack_arn, "stack")
        if observed_id != change_set_arn or (stack_region, stack_account, stack_in_arn, observed_stack) != (
            region,
            account,
            stack,
            stack,
        ):
            raise UnsupportedOperation("CloudFormation change set does not match the configured stack")
        if any(action not in {"Add", "Modify", "Remove", "Import"} for action, _, _, _ in changes):
            raise UnsupportedOperation("CloudFormation change set contains an unknown resource action")
        if any(replacement not in {"True", "False", "Conditional"} for _, _, _, replacement in changes):
            raise UnsupportedOperation("CloudFormation change set contains an unknown replacement state")
        if any(not isinstance(capability, str) for capability in capabilities):
            raise UnsupportedOperation("CloudFormation change set capabilities are invalid")
        summary = {
            "change_set_arn": observed_id,
            "stack_arn": stack_arn,
            "stack_name": stack,
            "account": account,
            "region": region,
            "status": status,
            "execution_status": execution_status,
            "changes": changes,
            "capabilities": capabilities,
            "include_nested_stacks": nested,
            "import_existing_resources": imports,
        }
        digest = sha256(json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        destructive = any(
            a == "Remove" or r in {"True", "Conditional"} or resource_type.startswith(("AWS::RDS::", "AWS::DynamoDB::"))
            for a, _, resource_type, r in changes
        )
        return ChangeSetReview(
            observed_id,
            stack_arn,
            stack,
            account,
            region,
            status,
            execution_status,
            changes,
            digest,
            destructive,
            capabilities,
            nested,
            imports,
        )

    def cloudformation_execute(
        self,
        project: Any,
        runner: Runner,
        change_set_arn: str,
        approved_digest: str,
        *,
        allow_destructive: bool = False,
    ) -> list[CommandSpec]:
        """Construct execution only after a fresh review matches the approved digest.

        The caller must separately enforce explicit production approval scoped to
        this account, region, stack, and change set before running the result.
        """
        review = self.cloudformation_review(project, runner, change_set_arn)
        if not re.fullmatch(r"[0-9a-f]{64}", approved_digest) or review.digest != approved_digest:
            raise UnsupportedOperation("CloudFormation change set changed since approval")
        if review.status != "CREATE_COMPLETE" or review.execution_status != "AVAILABLE":
            raise UnsupportedOperation("CloudFormation change set is not ready to execute")
        if not review.changes:
            raise UnsupportedOperation("CloudFormation change set has no resource changes")
        if review.capabilities:
            raise UnsupportedOperation("CloudFormation change sets with IAM or macro capabilities are unsupported")
        if review.include_nested_stacks or review.import_existing_resources:
            raise UnsupportedOperation("nested-stack or resource-import change sets are unsupported")
        if any(
            action == "Import" or resource_type.startswith(("AWS::IAM::", "AWS::CloudFormation::Stack", "Custom::"))
            for action, _, resource_type, _ in review.changes
        ):
            raise UnsupportedOperation(
                "CloudFormation import, IAM, nested stack, or custom resource changes are unsupported"
            )
        if review.destructive and not allow_destructive:
            raise UnsupportedOperation("resource deletion or replacement needs separate destructive approval")
        root = root_of(project)
        stack_check = CommandSpec(
            [
                "aws",
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                review.stack_name,
                "--query",
                "Stacks[0].{StackId:StackId,StackStatus:StackStatus}",
                "--output",
                "json",
                "--region",
                review.region,
                "--no-cli-pager",
            ],
            "confirm existing CloudFormation stack before executing reviewed update",
            False,
            cwd=root,
        )
        observed = runner.run(stack_check)
        if not observed.ok:
            raise UnsupportedOperation("CloudFormation stack could not be rechecked before execution")
        try:
            stack_state = json.loads(observed.stdout)
            current_id = stack_state["StackId"]
            current_status = stack_state["StackStatus"]
        except (ValueError, TypeError, KeyError) as exc:
            raise UnsupportedOperation("CloudFormation stack recheck response is invalid") from exc
        if current_id != review.stack_arn or current_status not in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
            "UPDATE_ROLLBACK_COMPLETE",
        }:
            raise UnsupportedOperation("CloudFormation stack changed or is not an existing updateable stack")
        return [
            CommandSpec(
                [
                    "aws",
                    "cloudformation",
                    "execute-change-set",
                    "--change-set-name",
                    change_set_arn,
                    "--stack-name",
                    review.stack_name,
                    "--region",
                    review.region,
                    "--no-disable-rollback",
                    "--no-cli-pager",
                ],
                f"execute reviewed CloudFormation change set {review.digest} for "
                f"{review.account}/{review.region}/{review.stack_name}",
                True,
                True,
                120,
                root,
            ),
            CommandSpec(
                [
                    "aws",
                    "cloudformation",
                    "wait",
                    "stack-update-complete",
                    "--stack-name",
                    review.stack_name,
                    "--region",
                    review.region,
                    "--no-cli-pager",
                ],
                "wait for CloudFormation stack update",
                False,
                timeout=900,
                cwd=root,
            ),
            CommandSpec(
                [
                    "aws",
                    "cloudformation",
                    "describe-stacks",
                    "--stack-name",
                    review.stack_name,
                    "--query",
                    "Stacks[0].{StackId:StackId,StackStatus:StackStatus}",
                    "--output",
                    "json",
                    "--region",
                    review.region,
                    "--no-cli-pager",
                ],
                "verify CloudFormation stack status",
                False,
                cwd=root,
            ),
        ]

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
