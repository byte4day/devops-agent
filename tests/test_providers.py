"""Provider contract tests use representative CLI output without cloud credentials."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devops_agent.providers import alibaba, all_providers, aws, cloudflare, github, netlify, vercel
from devops_agent.providers.base import CommandSpec, OperationResult, UnsupportedOperation


class FakeRunner:
    def __init__(self, replies: dict[tuple[str, ...], tuple[int, str, str]]):
        self.replies = replies
        self.commands: list[CommandSpec] = []

    def run(self, spec: CommandSpec) -> OperationResult:
        self.commands.append(spec)
        code, stdout, stderr = self.replies.get(tuple(spec.argv), (127, "", "not installed"))
        return OperationResult(spec, code, stdout, stderr)


def project(root: Path, **extra: object) -> dict[str, object]:
    return {
        "root": root,
        "name": "sample",
        "static": True,
        "api_routes": False,
        "output_dir": "dist",
        **extra,
    }


def test_registry_has_distinct_first_class_providers() -> None:
    providers = all_providers()
    assert {item.name for item in providers} == {
        "cloudflare",
        "aws",
        "alibaba",
        "vercel",
        "netlify",
        "github",
    }
    assert len({item.binary for item in providers}) == len(providers)


def test_cloudflare_detects_authenticated_account() -> None:
    runner = FakeRunner(
        {
            ("wrangler", "--version"): (0, "4.102.0\n", ""),
            ("wrangler", "whoami", "--json"): (
                0,
                json.dumps({"accounts": [{"id": "account-123", "name": "Team"}]}),
                "",
            ),
        }
    )
    status = cloudflare.Provider().detect(runner)
    assert status.authenticated and status.account == "account-123"
    assert all(not command.mutating for command in runner.commands)


def test_invalid_cloudflare_identity_is_not_authenticated() -> None:
    runner = FakeRunner(
        {
            ("wrangler", "--version"): (0, "4.102.0\n", ""),
            ("wrangler", "whoami", "--json"): (0, "not-json", ""),
        }
    )
    assert not cloudflare.Provider().detect(runner).authenticated


def test_cloudflare_workers_staging_requires_configured_environment(tmp_path: Path) -> None:
    (tmp_path / "wrangler.toml").write_text('name = "api"\nmain = "src/index.ts"\n', encoding="utf-8")
    provider = cloudflare.Provider()
    with pytest.raises(UnsupportedOperation, match="staging"):
        provider.plan(project(tmp_path, static=False), "staging")
    (tmp_path / "wrangler.toml").write_text(
        'name = "api"\nmain = "src/index.ts"\n[env.staging]\nname = "api-staging"\n',
        encoding="utf-8",
    )
    command = provider.plan(project(tmp_path, static=False), "staging")[0]
    assert command.argv == ["wrangler", "deploy", "--env", "staging", "--strict"]
    assert command.mutating and not command.production


def test_cloudflare_pages_production_requires_explicit_branch(tmp_path: Path) -> None:
    (tmp_path / "wrangler.toml").write_text('name = "web"\npages_build_output_dir = "dist"\n', encoding="utf-8")
    with pytest.raises(UnsupportedOperation, match="production_branch"):
        cloudflare.Provider().plan(project(tmp_path), "production")
    command = cloudflare.Provider().plan(project(tmp_path, production_branch="main"), "production")[0]
    assert command.argv == [
        "wrangler",
        "pages",
        "deploy",
        "dist",
        "--project-name",
        "web",
        "--branch",
        "main",
    ]


def test_cloudflare_jsonc_and_production_environment(tmp_path: Path) -> None:
    (tmp_path / "wrangler.jsonc").write_text(
        '{// comment\n"name":"api", "main":"src/index.ts", "env":{"production":{},},}',
        encoding="utf-8",
    )
    command = cloudflare.Provider().plan(project(tmp_path, static=False), "production")[0]
    assert command.argv == ["wrangler", "deploy", "--env", "production", "--strict"]


def test_aws_s3_upload_is_scoped_and_non_deleting(tmp_path: Path) -> None:
    target = project(
        tmp_path,
        aws_bucket="example-site-bucket",
        aws_account_id="123456789012",
        aws_region="us-east-1",
    )
    provider = aws.Provider()
    command = provider.plan(target, "production")[0]
    assert command.argv[:4] == ["aws", "s3", "sync", str(tmp_path / "dist") + "/"]
    assert command.production and "--no-follow-symlinks" in command.argv
    assert "--delete" not in command.argv
    assert "cloudformation" not in command.argv
    preflight = provider.preflight(target)
    assert len(preflight) == 1 and "--expected-bucket-owner" in preflight[0].argv
    assert not preflight[0].mutating


def test_aws_requires_account_and_isolated_preview_prefix(tmp_path: Path) -> None:
    provider = aws.Provider()
    with pytest.raises(UnsupportedOperation, match="aws_account_id"):
        provider.plan(project(tmp_path, aws_bucket="example-site-bucket"), "production")
    with pytest.raises(UnsupportedOperation, match="isolated"):
        provider.plan(
            project(tmp_path, aws_bucket="example-site-bucket", aws_account_id="123456789012"),
            "preview",
        )


def test_static_upload_rejects_repository_root_as_publish_directory(tmp_path: Path) -> None:
    target = project(
        tmp_path,
        output_dir=".",
        aws_bucket="example-site-bucket",
        aws_account_id="123456789012",
    )
    with pytest.raises(UnsupportedOperation, match="output directory"):
        aws.Provider().plan(target, "production")


def test_alibaba_detects_identity_and_ossutil_capability() -> None:
    runner = FakeRunner(
        {
            ("aliyun", "version"): (0, "3.5.1\n", ""),
            ("aliyun", "sts", "GetCallerIdentity"): (
                0,
                '{"AccountId":"123456789012","Arn":"acs:sts::123456789012:assumed-role/test"}',
                "",
            ),
            ("aliyun", "configure", "get", "region"): (0, "cn-hangzhou\n", ""),
            ("ossutil", "version"): (0, "ossutil version 2.4.0\n", ""),
        }
    )
    status = alibaba.Provider().detect(runner)
    assert status.authenticated and status.account == "123456789012"
    assert status.region == "cn-hangzhou" and "oss_static_upload" in status.capabilities


def test_alibaba_oss_upload_requires_explicit_target(tmp_path: Path) -> None:
    provider = alibaba.Provider()
    with pytest.raises(UnsupportedOperation, match="oss_bucket"):
        provider.plan(project(tmp_path), "production")
    command = provider.plan(project(tmp_path, oss_bucket="example-bucket", oss_prefix="staging/"), "staging")[0]
    assert command.argv == [
        "ossutil",
        "cp",
        "-r",
        str(tmp_path / "dist") + "/",
        "oss://example-bucket/staging/",
    ]
    assert command.mutating and not command.production


def test_vercel_requires_link_and_parses_observed_url(tmp_path: Path) -> None:
    provider = vercel.Provider()
    with pytest.raises(UnsupportedOperation, match="linked"):
        provider.plan(project(tmp_path), "preview")
    link = tmp_path / ".vercel" / "project.json"
    link.parent.mkdir()
    link.write_text('{"projectId":"prj_test","orgId":"team_test"}', encoding="utf-8")
    command = provider.plan(project(tmp_path), "production")[0]
    assert command.argv == ["vercel", "deploy", "--yes", "--logs", "--prod"]
    parsed = provider.parse_deployment(OperationResult(command, 0, "https://sample-abc.vercel.app\n"))
    assert parsed.url == "https://sample-abc.vercel.app"


def test_netlify_linked_deploy_and_known_release_rollback(tmp_path: Path) -> None:
    provider = netlify.Provider()
    link = tmp_path / ".netlify" / "state.json"
    link.parent.mkdir()
    link.write_text('{"siteId":"site-123"}', encoding="utf-8")
    command = provider.plan(project(tmp_path), "preview")[0]
    assert command.argv == [
        "netlify",
        "deploy",
        "--site",
        "site-123",
        "--json",
        "--dir",
        "dist",
        "--no-build",
    ]
    parsed = provider.parse_deployment(
        OperationResult(
            command,
            0,
            '{"id":"0123456789abcdef01234567","deploy_ssl_url":"https://preview.netlify.app"}',
        )
    )
    assert parsed.url == "https://preview.netlify.app" and parsed.release == "0123456789abcdef01234567"
    rollback = provider.rollback(project(tmp_path), release="0123456789abcdef01234567")[0]
    assert rollback.production and rollback.mutating and "restoreSiteDeploy" in rollback.argv
    with pytest.raises(UnsupportedOperation):
        provider.rollback(project(tmp_path), release="--help")


def test_netlify_nextjs_uses_framework_build_instead_of_raw_next_directory(tmp_path: Path) -> None:
    link = tmp_path / ".netlify" / "state.json"
    link.parent.mkdir()
    link.write_text('{"siteId":"site-123"}', encoding="utf-8")
    command = netlify.Provider().plan(
        project(tmp_path, framework="Next.js", static=False, api_routes=True, output_dir=".next"),
        "preview",
    )[0]
    assert "--dir" not in command.argv and "--no-build" not in command.argv
    assert command.argv == ["netlify", "deploy", "--site", "site-123", "--json"]


def test_netlify_auth_requires_valid_identity() -> None:
    runner = FakeRunner(
        {
            ("netlify", "--version"): (0, "netlify-cli/23.0.0", ""),
            ("netlify", "api", "getCurrentUser"): (0, '{"error":"unauthorized"}', ""),
        }
    )
    assert not netlify.Provider().detect(runner).authenticated
    runner.replies[("netlify", "api", "getCurrentUser")] = (
        0,
        '{"id":"user-123","email":"dev@example.com"}',
        "",
    )
    assert netlify.Provider().detect(runner).account == "dev@example.com"


def test_partial_deployment_failure_does_not_claim_url() -> None:
    spec = CommandSpec(["netlify", "deploy"], "deploy", True)
    result = OperationResult(spec, 1, '{"deploy_ssl_url":"https://broken.netlify.app"}', "failed")
    parsed = netlify.Provider().parse_deployment(result)
    assert parsed.url is None and parsed.release is None


def test_github_json_auth_requires_success_state() -> None:
    payload = {
        "hosts": {"github.com": [{"active": True, "login": "user", "state": "error", "error": "Bad credentials"}]}
    }
    runner = FakeRunner(
        {
            ("gh", "--version"): (0, "gh version 2.0.0\n", ""),
            ("gh", "auth", "status", "--active", "--json", "hosts"): (0, json.dumps(payload), ""),
        }
    )
    assert not github.Provider().detect(runner).authenticated
    payload["hosts"]["github.com"][0]["state"] = "success"
    runner.replies[("gh", "auth", "status", "--active", "--json", "hosts")] = (
        0,
        json.dumps(payload),
        "",
    )
    assert github.Provider().detect(runner).account == "user"


def test_github_diagnosis_only_reads_failed_run(tmp_path: Path) -> None:
    commands = github.Provider().ci_diagnose(project(tmp_path), "12345")
    assert all(not command.mutating for command in commands)
    assert commands[1].argv == ["gh", "run", "view", "12345", "--log-failed"]
    with pytest.raises(UnsupportedOperation):
        github.Provider().ci_diagnose(project(tmp_path), "12345; echo leak")


def test_all_providers_reject_generic_rollback_without_release(tmp_path: Path) -> None:
    (tmp_path / "wrangler.toml").write_text('name = "api"\nmain = "src/index.ts"\n', encoding="utf-8")
    (tmp_path / ".vercel").mkdir()
    (tmp_path / ".vercel" / "project.json").write_text('{"projectId":"p","orgId":"o"}', encoding="utf-8")
    (tmp_path / ".netlify").mkdir()
    (tmp_path / ".netlify" / "state.json").write_text('{"siteId":"s"}', encoding="utf-8")
    for provider in (
        cloudflare.Provider(),
        vercel.Provider(),
        netlify.Provider(),
        aws.Provider(),
        alibaba.Provider(),
        github.Provider(),
    ):
        with pytest.raises(UnsupportedOperation):
            provider.rollback(project(tmp_path))
