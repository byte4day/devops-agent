from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from devops_agent.core.analyzer import analyze
from devops_agent.core.approvals import ApprovalRequired
from devops_agent.core.config import load_config
from devops_agent.core.diagnostics import classify
from devops_agent.core.executor import execute
from devops_agent.core.planner import choose_provider, make_plan
from devops_agent.core.security import SubprocessRunner, redact
from devops_agent.core.verifier import Check, Verification
from devops_agent.providers import aws
from devops_agent.providers.base import CommandSpec, OperationResult, ProviderStatus


def test_analyze_next_with_api_and_database_without_reading_secret_values(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "site",
                "dependencies": {"next": "15.0", "pg": "1"},
                "scripts": {"build": "next build", "test": "vitest run"},
            }
        )
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'")
    (tmp_path / "app/api").mkdir(parents=True)
    (tmp_path / ".env").write_text("DATABASE_URL=do-not-read-this")
    (tmp_path / ".env.example").write_text("DATABASE_URL=\nPUBLIC_URL=\n")
    result = analyze(tmp_path)
    assert result.framework == "Next.js"
    assert result.static is False and result.api_routes is True
    assert result.package_manager == "pnpm"
    assert result.database_dependencies == ("PostgreSQL",)
    assert result.env_names == ("DATABASE_URL", "PUBLIC_URL")
    assert "do-not-read-this" not in str(result)


def test_analyze_static_and_unknown(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>ok</h1>")
    assert analyze(tmp_path).static is True
    (tmp_path / "index.html").unlink()
    assert analyze(tmp_path).framework == "Unknown"


def test_analyze_vite_react_and_astro_server(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"vite": "6", "react": "19"}}))
    assert analyze(tmp_path).framework == "React + Vite"
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"astro": "5"}}))
    (tmp_path / "astro.config.mjs").write_text("export default { output: 'server' }")
    result = analyze(tmp_path)
    assert result.framework == "Astro" and not result.static


def test_analyze_malformed_manifest_fields_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": ["vite"], "devDependencies": None, "scripts": ["build"]})
    )
    assert analyze(tmp_path).framework == "Node.js"


def test_config_rejects_unsafe_commands_and_production_bypass(tmp_path: Path) -> None:
    (tmp_path / "devops.yaml").write_text("build_commands:\n  - npm run build\n")
    with pytest.raises(ValueError, match="argument arrays"):
        load_config(tmp_path)
    (tmp_path / "devops.yaml").write_text("approval_policy:\n  require_production_approval: false\n")
    with pytest.raises(ValueError, match="cannot waive"):
        load_config(tmp_path)
    (tmp_path / "devops.yaml").write_text("max_repair_attempts: 4\n")
    with pytest.raises(ValueError, match="0 to 3"):
        load_config(tmp_path)
    (tmp_path / "devops.yaml").write_text("provider: !python/object/apply:os.system ['echo bad']\n")
    with pytest.raises(ValueError, match="Invalid devops.yaml"):
        load_config(tmp_path)
    (tmp_path / "devops.yaml").write_text("build_commands:\n  - [tool, '--password', secret-value]\n")
    with pytest.raises(ValueError, match="credential arguments"):
        load_config(tmp_path)
    (tmp_path / "devops.yaml").write_text("output_dir: ../private\n")
    with pytest.raises(ValueError, match="safe relative"):
        load_config(tmp_path)
    (tmp_path / "devops.yaml").write_text(
        "output_dir: public-dist\ncloudformation_template: infra.yaml\ncloudformation_stack: app-stack\n"
    )
    config = load_config(tmp_path)
    assert config.output_dir == "public-dist" and config.cloudformation_stack == "app-stack"


def test_redaction_and_classification() -> None:
    fixture_token = "ghp_" + "a" * 24
    safe = redact(f"AWS_SECRET_ACCESS_KEY=abc123 Authorization: Bearer token123 {fixture_token}")
    assert "abc123" not in safe and "token123" not in safe and "ghp_" not in safe
    issue = classify("Error: DATABASE_URL environment variable not set", 1)
    assert issue.category == "MISSING_ENVIRONMENT_VARIABLE"
    assert classify("Timed out", 124).category == "DEPLOYMENT_TIMEOUT"


def test_runner_never_interprets_shell_metacharacters(tmp_path: Path) -> None:
    runner = SubprocessRunner(tmp_path)
    marker = tmp_path / "owned"
    result = runner.run(
        CommandSpec(
            ["python3", "-c", "import sys; print(sys.argv[1])", f"x;touch {marker}"],
            "literal argument",
            False,
        )
    )
    assert result.ok and "x;touch" in result.stdout
    assert not marker.exists()
    with pytest.raises(ValueError, match="Shell"):
        runner.run(CommandSpec(["sh", "-c", "true"], "shell", False))


class FakeProvider:
    name = "fake"

    def plan(self, project, environment):
        return [CommandSpec(["fake", "deploy"], "deploy fixture", True, environment == "production")]

    def parse_deployment(self, result):
        return result


class FakeRunner:
    def __init__(self, root: Path, *, fail: bool = False):
        self.cwd = root
        self.calls = []
        self.fail = fail

    def run(self, spec):
        self.calls.append(spec)
        return OperationResult(spec, 1 if self.fail else 0, "", "build failed" if self.fail else "")


def test_plan_and_production_approval_guard(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ok")
    project = analyze(tmp_path)
    status = ProviderStatus("fake", "fake", True, "1.0", True, "acct")
    plan = make_plan(project, load_config(tmp_path), FakeProvider(), status, "production")
    runner = FakeRunner(tmp_path)
    with pytest.raises(ApprovalRequired):
        execute(plan, FakeProvider(), status, runner)
    assert not runner.calls
    assert plan.approval_phrase is not None and "@acct#" in plan.approval_phrase
    result = execute(plan, FakeProvider(), status, runner, approve=plan.approval_phrase)
    assert result.verdict == "UNVERIFIED"
    assert result.phases["deployment"] == "PASSED"


def test_approval_phrase_changes_with_account_and_deployment_target(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ok")
    project = analyze(tmp_path)
    first = ProviderStatus("fake", "fake", True, "1", True, "account-one")
    second = ProviderStatus("fake", "fake", True, "1", True, "account-two")
    config = load_config(tmp_path)
    first_plan = make_plan(project, config, FakeProvider(), first, "production")
    assert (
        first_plan.approval_phrase != make_plan(project, config, FakeProvider(), second, "production").approval_phrase
    )

    class ChangedTarget(FakeProvider):
        def plan(self, project, environment):
            return [CommandSpec(["fake", "deploy", "other-target"], "deploy fixture", True, True)]

    assert (
        first_plan.approval_phrase != make_plan(project, config, ChangedTarget(), first, "production").approval_phrase
    )


def test_account_change_blocks_deployment(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ok")
    project = analyze(tmp_path)
    first = ProviderStatus("fake", "fake", True, "1", True, "account-one")
    changed = ProviderStatus("fake", "fake", True, "1", True, "account-two")
    plan = make_plan(project, load_config(tmp_path), FakeProvider(), first, "preview")
    runner = FakeRunner(tmp_path)
    with pytest.raises(PermissionError, match="changed"):
        execute(plan, FakeProvider(), changed, runner)
    assert not runner.calls


def test_failed_deployment_stops_and_reports_observed_error(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ok")
    project = analyze(tmp_path)
    status = ProviderStatus("fake", "fake", True, "1", True, "acct")
    plan = make_plan(project, load_config(tmp_path), FakeProvider(), status, "preview")
    report = execute(plan, FakeProvider(), status, FakeRunner(tmp_path, fail=True))
    assert report.verdict == "FAILED" and report.diagnostic.category == "BUILD_FAILURE"
    assert report.phases["deployment"] == "FAILED"


def test_provider_choice_requires_unambiguous_target(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ok")
    project = analyze(tmp_path)
    statuses = [
        ProviderStatus("vercel", "vercel", True, "1", True),
        ProviderStatus("netlify", "netlify", True, "1", True),
    ]
    with pytest.raises(ValueError, match="unambiguous"):
        choose_provider(project, load_config(tmp_path), statuses, None)


def test_aws_plan_reads_validated_config_without_optional_prefix(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"vite": "6"}, "scripts": {"build": "vite build"}})
    )
    (tmp_path / "devops.yaml").write_text(
        "provider: aws\naws_bucket: example-site-bucket\naws_account_id: '123456789012'\n"
    )
    project = analyze(tmp_path)
    status = ProviderStatus("aws", "aws", True, "2", True, "123456789012")
    plan = make_plan(project, load_config(tmp_path), aws.Provider(), status, "production")
    assert "--delete" not in plan.deploy[0].argv
    assert plan.preflight[0].argv[:3] == ["aws", "s3api", "head-bucket"]


def test_api_app_needs_application_health_even_when_homepage_responds(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"next": "15"}}))
    (tmp_path / "app/api").mkdir(parents=True)
    project = analyze(tmp_path)
    status = ProviderStatus("fake", "fake", True, "1", True, "acct")
    plan = make_plan(project, load_config(tmp_path), FakeProvider(), status, "preview")

    class UrlProvider(FakeProvider):
        def parse_deployment(self, result):
            return replace(result, url="https://preview.example.com")

    monkeypatch.setattr(
        "devops_agent.core.executor.verify",
        lambda url: Verification(url, (Check("http", "PASSED", "HTTP 200"),), "HEALTHY"),
    )
    report = execute(plan, UrlProvider(), status, FakeRunner(tmp_path))
    assert report.phases["health"] == "PASSED"
    assert report.phases["application_health"] == "NOT_TESTED"
    assert report.verdict == "UNVERIFIED"
