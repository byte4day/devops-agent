"""Read-only inspection and bounded domain-change proposal contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devops_agent.providers import cloudflare, netlify, vercel
from devops_agent.providers.base import CommandSpec, UnsupportedOperation


def _project(root: Path) -> dict[str, object]:
    return {"root": root, "static": True, "api_routes": False, "output_dir": "dist"}


def _link_vercel(root: Path) -> None:
    path = root / ".vercel" / "project.json"
    path.parent.mkdir()
    path.write_text('{"projectId":"prj_123","orgId":"team_456"}', encoding="utf-8")


def _link_netlify(root: Path) -> None:
    path = root / ".netlify" / "state.json"
    path.parent.mkdir()
    path.write_text('{"siteId":"site-123"}', encoding="utf-8")


def _assert_read_only(specs: list[CommandSpec]) -> None:
    assert specs
    assert all(not spec.mutating and not spec.production for spec in specs)


def test_cloudflare_pages_inspection_uses_read_only_wrangler_commands(tmp_path: Path) -> None:
    (tmp_path / "wrangler.toml").write_text('name = "sample"\npages_build_output_dir = "dist"\n', encoding="utf-8")
    provider = cloudflare.Provider()
    project = _project(tmp_path)
    project_commands = provider.inspect_project(project)
    assert project_commands[0].argv == ["wrangler", "pages", "project", "list", "--json"]
    assert provider.inspect_domains(project) == project_commands
    _assert_read_only(project_commands)
    environment = provider.inspect_environment(project, "production")
    assert environment[0].argv == [
        "wrangler",
        "pages",
        "deployment",
        "list",
        "--project-name",
        "sample",
        "--environment",
        "production",
        "--json",
    ]
    _assert_read_only(environment)
    with pytest.raises(UnsupportedOperation, match="preview and production"):
        provider.inspect_environment(project, "staging")
    with pytest.raises(UnsupportedOperation, match="no safe additive Wrangler command"):
        provider.plan_domain_change(project, "www.example.com", "sample")


def test_cloudflare_workers_inspection_rejects_unverified_domain_listing(tmp_path: Path) -> None:
    (tmp_path / "wrangler.toml").write_text(
        'name = "api"\nmain = "src/index.ts"\n[env.staging]\nname = "api-staging"\n',
        encoding="utf-8",
    )
    provider = cloudflare.Provider()
    project = _project(tmp_path)
    assert provider.inspect_project(project)[0].argv == ["wrangler", "deployments", "status", "--json"]
    staging = provider.inspect_environment(project, "staging")
    assert staging[0].argv == ["wrangler", "deployments", "list", "--json", "--env", "staging"]
    _assert_read_only(staging)
    with pytest.raises(UnsupportedOperation, match="read-only Worker custom-domain"):
        provider.inspect_domains(project)


def test_vercel_inspection_and_additive_domain_proposal(tmp_path: Path) -> None:
    _link_vercel(tmp_path)
    provider = vercel.Provider()
    project = _project(tmp_path)
    assert provider.inspect_project(project)[0].argv == ["vercel", "project", "inspect"]
    assert provider.inspect_domains(project)[0].argv == ["vercel", "domains", "ls"]
    assert provider.inspect_environment(project, "production")[0].argv == ["vercel", "list", "--prod"]
    _assert_read_only(provider.inspect_environment(project, "preview"))
    proposal = provider.plan_domain_change(project, "WWW.Example.com.", "sample-web")
    assert proposal["hostname"] == "www.example.com"
    assert proposal["target"] == "sample-web"
    assert proposal["approval_required"] is True
    assert proposal["expected_project_id"] == "prj_123"
    assert proposal["expected_org_id"] == "team_456"
    inspection = proposal["inspection"]
    mutation = proposal["mutation"]
    assert isinstance(inspection, list) and isinstance(mutation, list)
    _assert_read_only(inspection)
    assert inspection[0].argv == ["vercel", "project", "inspect", "sample-web"]
    assert inspection[1].argv == ["vercel", "domains", "inspect", "www.example.com"]
    assert len(mutation) == 1
    assert mutation[0].argv == ["vercel", "domains", "add", "www.example.com", "sample-web"]
    assert mutation[0].mutating and mutation[0].production
    assert "--force" not in mutation[0].argv


@pytest.mark.parametrize("hostname", ["--force", "*.example.com", "a..example.com", "a.example.com;id"])
def test_vercel_domain_proposal_rejects_unsafe_hostnames(tmp_path: Path, hostname: str) -> None:
    _link_vercel(tmp_path)
    with pytest.raises(UnsupportedOperation, match="valid ASCII hostname"):
        vercel.Provider().plan_domain_change(_project(tmp_path), hostname, "sample-web")


def test_vercel_domain_proposal_rejects_unlinked_or_unsafe_target(tmp_path: Path) -> None:
    project = _project(tmp_path)
    with pytest.raises(UnsupportedOperation, match="not linked"):
        vercel.Provider().plan_domain_change(project, "www.example.com", "sample-web")
    _link_vercel(tmp_path)
    with pytest.raises(UnsupportedOperation, match="valid project name"):
        vercel.Provider().plan_domain_change(project, "www.example.com", "other;command")


def test_netlify_inspection_never_reads_environment_secret_values(tmp_path: Path) -> None:
    _link_netlify(tmp_path)
    provider = netlify.Provider()
    project = _project(tmp_path)
    site = provider.inspect_project(project)
    assert site == provider.inspect_domains(project)
    assert site[0].argv[:3] == ["netlify", "api", "getSite"]
    assert json.loads(site[0].argv[-1]) == {"site_id": "site-123"}
    _assert_read_only(site)
    prod = provider.inspect_environment(project, "production")
    assert prod[0].argv[:3] == ["netlify", "api", "listSiteDeploys"]
    assert json.loads(prod[0].argv[-1]) == {
        "site_id": "site-123",
        "production": True,
        "per_page": 10,
    }
    _assert_read_only(prod)
    assert "env:list" not in " ".join(prod[0].argv)
    with pytest.raises(UnsupportedOperation, match="no distinct staging"):
        provider.inspect_environment(project, "staging")
    with pytest.raises(UnsupportedOperation, match="no verified additive"):
        provider.plan_domain_change(project, "www.example.com", "site-123")
