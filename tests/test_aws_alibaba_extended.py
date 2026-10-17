"""Offline checks for reviewed AWS change sets and bounded Alibaba diagnostics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devops_agent.providers import alibaba, aws
from devops_agent.providers.base import CommandSpec, OperationResult, UnsupportedOperation

ACCOUNT = "123456789012"
REGION = "us-east-1"
STACK = "sample-stack"
STACK_ARN = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK}/abc123"
CHANGESET_ARN = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/release-42/def456"


class CloudFormationRunner:
    def __init__(self, *, account: str = ACCOUNT, changes: list[dict[str, str]] | None = None):
        self.account = account
        self.changes = changes or [
            {"Action": "Modify", "LogicalResourceId": "Web", "ResourceType": "AWS::S3::Bucket", "Replacement": "False"}
        ]
        self.commands: list[CommandSpec] = []
        self.status = "CREATE_COMPLETE"
        self.execution_status = "AVAILABLE"
        self.stack_status = "UPDATE_COMPLETE"
        self.capabilities: list[str] = []
        self.nested = False

    def run(self, spec: CommandSpec) -> OperationResult:
        self.commands.append(spec)
        action = spec.argv[2]
        if action == "get-caller-identity":
            return OperationResult(spec, 0, self.account + "\n")
        if action == "describe-stacks":
            return OperationResult(
                spec,
                0,
                json.dumps({"StackId": STACK_ARN, "StackStatus": self.stack_status, "ParameterKeys": ["DbPassword"]}),
            )
        if action == "describe-change-set":
            return OperationResult(
                spec,
                0,
                json.dumps(
                    {
                        "ChangeSetId": CHANGESET_ARN,
                        "StackId": STACK_ARN,
                        "StackName": STACK,
                        "Status": self.status,
                        "ExecutionStatus": self.execution_status,
                        "Capabilities": self.capabilities,
                        "IncludeNestedStacks": self.nested,
                        "Changes": self.changes,
                    }
                ),
            )
        return OperationResult(spec, 127, stderr="unmocked command")


def target(tmp_path: Path) -> dict[str, object]:
    (tmp_path / "template.yaml").write_text("Resources: {}\n", encoding="utf-8")
    return {
        "root": tmp_path,
        "cloudformation_template": "template.yaml",
        "cloudformation_stack": STACK,
        "aws_region": REGION,
        "aws_account_id": ACCOUNT,
    }


def test_prepare_creates_only_update_changeset_and_preserves_parameter_keys(tmp_path: Path) -> None:
    runner = CloudFormationRunner()
    commands = aws.Provider().cloudformation_prepare(target(tmp_path), runner, "release-42")
    assert len(commands) == 2
    assert commands[0].argv[:3] == ["aws", "cloudformation", "create-change-set"]
    assert commands[0].mutating and commands[0].production
    assert commands[0].argv[commands[0].argv.index("--change-set-type") + 1] == "UPDATE"
    assert "execute-change-set" not in commands[0].argv
    assert json.loads(commands[0].argv[commands[0].argv.index("--parameters") + 1]) == [
        {"ParameterKey": "DbPassword", "UsePreviousValue": True}
    ]
    assert all(not spec.mutating for spec in runner.commands)
    assert "ParameterValue" not in " ".join(str(spec.argv) for spec in runner.commands)


def test_prepare_refuses_wrong_active_account(tmp_path: Path) -> None:
    runner = CloudFormationRunner(account="999999999999")
    with pytest.raises(UnsupportedOperation, match="identity"):
        aws.Provider().cloudformation_prepare(target(tmp_path), runner, "release-42")
    assert len(runner.commands) == 1


def test_review_is_target_bound_and_secret_minimized(tmp_path: Path) -> None:
    runner = CloudFormationRunner()
    review = aws.Provider().cloudformation_review(target(tmp_path), runner, CHANGESET_ARN)
    assert review.account == ACCOUNT and review.region == REGION and review.stack_name == STACK
    assert len(review.digest) == 64 and not review.destructive
    query = runner.commands[-1].argv[runner.commands[-1].argv.index("--query") + 1]
    assert "Parameters" not in query and "Outputs" not in query and "Details" not in query
    with pytest.raises(UnsupportedOperation, match="different account or region"):
        aws.Provider().cloudformation_review(target(tmp_path), runner, CHANGESET_ARN.replace(ACCOUNT, "999999999999"))


def test_execute_requires_fresh_matching_review_and_ready_status(tmp_path: Path) -> None:
    project = target(tmp_path)
    runner = CloudFormationRunner()
    provider = aws.Provider()
    digest = provider.cloudformation_review(project, runner, CHANGESET_ARN).digest
    commands = provider.cloudformation_execute(project, runner, CHANGESET_ARN, digest)
    assert commands[0].argv[:3] == ["aws", "cloudformation", "execute-change-set"]
    assert commands[0].mutating and commands[0].production
    assert commands[1].argv[:4] == ["aws", "cloudformation", "wait", "stack-update-complete"]
    assert all(not command.mutating for command in runner.commands)
    runner.execution_status = "OBSOLETE"
    with pytest.raises(UnsupportedOperation, match="changed since approval"):
        provider.cloudformation_execute(project, runner, CHANGESET_ARN, digest)


def test_execute_blocks_replacement_without_separate_destructive_approval(tmp_path: Path) -> None:
    project = target(tmp_path)
    runner = CloudFormationRunner(
        changes=[
            {
                "Action": "Modify",
                "LogicalResourceId": "Database",
                "ResourceType": "AWS::RDS::DBInstance",
                "Replacement": "True",
            }
        ]
    )
    provider = aws.Provider()
    digest = provider.cloudformation_review(project, runner, CHANGESET_ARN).digest
    with pytest.raises(UnsupportedOperation, match="destructive approval"):
        provider.cloudformation_execute(project, runner, CHANGESET_ARN, digest)
    assert provider.cloudformation_execute(project, runner, CHANGESET_ARN, digest, allow_destructive=True)[0].production


def test_review_accepts_add_without_replacement_field(tmp_path: Path) -> None:
    runner = CloudFormationRunner(
        changes=[{"Action": "Add", "LogicalResourceId": "Web", "ResourceType": "AWS::S3::Bucket"}]
    )
    review = aws.Provider().cloudformation_review(target(tmp_path), runner, CHANGESET_ARN)
    assert review.changes[0][-1] == "False"
    assert not review.destructive


def test_execute_rejects_create_stack_state_and_iam_capability(tmp_path: Path) -> None:
    project = target(tmp_path)
    runner = CloudFormationRunner()
    provider = aws.Provider()
    digest = provider.cloudformation_review(project, runner, CHANGESET_ARN).digest
    runner.stack_status = "REVIEW_IN_PROGRESS"
    with pytest.raises(UnsupportedOperation, match="existing updateable stack"):
        provider.cloudformation_execute(project, runner, CHANGESET_ARN, digest)
    runner.stack_status = "UPDATE_COMPLETE"
    runner.capabilities = ["CAPABILITY_NAMED_IAM"]
    digest = provider.cloudformation_review(project, runner, CHANGESET_ARN).digest
    with pytest.raises(UnsupportedOperation, match="IAM or macro"):
        provider.cloudformation_execute(project, runner, CHANGESET_ARN, digest)


def test_alibaba_ecs_diagnostic_is_scoped_and_read_only(tmp_path: Path) -> None:
    command = alibaba.Provider().diagnose_ecs({"root": tmp_path}, "i-bp1234567890", "cn-hangzhou")[0]
    assert command.argv[:5] == ["aliyun", "ecs", "DescribeInstances", "--RegionId", "cn-hangzhou"]
    assert command.argv[command.argv.index("--InstanceIds") + 1] == '["i-bp1234567890"]'
    assert not command.mutating
    with pytest.raises(UnsupportedOperation):
        alibaba.Provider().diagnose_ecs({"root": tmp_path}, "i-bp1; rm -rf /", "cn-hangzhou")


def test_alibaba_oss_inspection_is_bounded(tmp_path: Path) -> None:
    project = {"root": tmp_path, "oss_bucket": "sample-bucket", "oss_prefix": "preview/"}
    provider = alibaba.Provider()
    assert "--limited-num=50" in provider.preflight(project)[0].argv
    assert "--limited-num=50" in provider.logs(project)[0].argv
