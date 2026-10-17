from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from devops_agent.cli import main
from devops_agent.providers.base import CommandSpec, OperationResult, ProviderStatus


def _git(root: Path, *args: str) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, *args], cwd=root, check=True, capture_output=True)  # noqa: S603


def test_ci_setup_and_inspect_cli_without_remote_auth(tmp_path: Path, capsys) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node --test"}}')
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion":3}')
    assert main(["--project", str(tmp_path), "ci", "setup"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["created"] is True
    assert main(["--project", str(tmp_path), "ci", "inspect"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["has_ci_job"] is True


def test_ci_inspect_cli_fails_for_invalid_trigger(tmp_path: Path, capsys) -> None:
    folder = tmp_path / ".github" / "workflows"
    folder.mkdir(parents=True)
    (folder / "broken.yml").write_text("on: []\njobs:\n  test:\n    runs-on: ubuntu-latest\n")
    assert main(["--project", str(tmp_path), "ci", "inspect"]) == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False


def test_ci_repair_cli_diagnoses_local_evidence(tmp_path: Path, capsys) -> None:
    log = tmp_path / "failure.log"
    log.write_text("Error: Cannot find module 'missing-package'\n")
    assert main(["--project", str(tmp_path), "ci", "repair", "--log-file", str(log)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["category"] == "DEPENDENCY_FAILURE"
    assert result["changes_applied"] is False


def test_fix_cli_reports_budget_without_editing_project(tmp_path: Path, capsys) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "app.py").write_text("print('ok')\n")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "initial")
    log = tmp_path / "build.log"
    log.write_text("Build failed: SyntaxError\n")
    assert main(["--project", str(tmp_path), "fix", "--incident", "build-1", "--log-file", str(log)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["diagnostic"]["category"] == "BUILD_FAILURE"
    assert result["attempts_remaining"] == 3
    assert result["changes_applied"] is False


def test_infra_prepare_needs_exact_target_approval_before_mutation(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "devops.yaml").write_text(
        "aws_account_id: '123456789012'\naws_region: us-east-1\ncloudformation_stack: sample-stack\n"
    )
    calls: list[CommandSpec] = []

    class FakeRunner:
        def __init__(self, cwd):
            self.cwd = cwd

        def run(self, spec):
            calls.append(spec)
            return OperationResult(spec, 0, "{}")

    class FakeAws:
        name = "aws"

        def detect(self, runner):
            return ProviderStatus("aws", "aws", True, "2", True, "123456789012", "us-east-1")

        def cloudformation_prepare(self, target, runner, name):
            assert name == "release-1"
            return [CommandSpec(["aws", "cloudformation", "create-change-set"], "prepare", True, True)]

    monkeypatch.setattr("devops_agent.cli.all_providers", lambda: [FakeAws()])
    monkeypatch.setattr("devops_agent.cli.SubprocessRunner", FakeRunner)
    argv = ["--project", str(tmp_path), "infra", "prepare", "--change-set-name", "release-1"]
    assert main(argv) == 2
    assert "Approval required" in json.loads(capsys.readouterr().out)["error"]
    assert calls == []
    approval = "infra-prepare:aws:123456789012/us-east-1/sample-stack:production"
    assert main([*argv, "--approve", approval]) == 0
    assert len(calls) == 1 and calls[0].mutating


def test_read_only_cli_returns_failure_when_provider_read_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    class FailingRunner:
        def __init__(self, cwd):
            self.cwd = cwd

        def run(self, spec):
            return OperationResult(spec, 1, stderr="permission denied")

    class FailingProvider:
        name = "fixture"

        def detect(self, runner):
            return ProviderStatus("fixture", "fixture", True, "1", True, "account")

        def logs(self, target, *, release=None):
            return [CommandSpec(["fixture", "logs"], "read logs", False)]

    monkeypatch.setattr("devops_agent.cli.all_providers", lambda: [FailingProvider()])
    monkeypatch.setattr("devops_agent.cli.SubprocessRunner", FailingRunner)
    assert main(["--project", str(tmp_path), "logs", "--provider", "fixture"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result[0]["returncode"] == 1
    assert result[0]["diagnostic"]["category"] == "PERMISSION_DENIED"


def test_infra_validate_propagates_failed_aws_read(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "devops.yaml").write_text("aws_account_id: '123456789012'\n")

    class FailingRunner:
        def __init__(self, cwd):
            self.cwd = cwd

        def run(self, spec):
            return OperationResult(spec, 1, stderr="invalid template")

    class FakeAws:
        name = "aws"

        def detect(self, runner):
            return ProviderStatus("aws", "aws", True, "2", True, "123456789012")

        def validate_cloudformation(self, target):
            return [CommandSpec(["aws", "cloudformation", "validate-template"], "validate", False)]

    monkeypatch.setattr("devops_agent.cli.all_providers", lambda: [FakeAws()])
    monkeypatch.setattr("devops_agent.cli.SubprocessRunner", FailingRunner)
    assert main(["--project", str(tmp_path), "infra", "validate"]) == 1
    assert json.loads(capsys.readouterr().out)[0]["returncode"] == 1
