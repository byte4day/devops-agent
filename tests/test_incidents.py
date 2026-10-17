from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from devops_agent.core.analyzer import analyze
from devops_agent.core.config import load_config
from devops_agent.core.executor import execute
from devops_agent.core.incidents import IncidentLedger, candidate_fingerprint
from devops_agent.core.planner import make_plan
from devops_agent.providers.base import CommandSpec, OperationResult, ProviderStatus


def _git(root: Path, *args: str) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, *args], cwd=root, check=True, capture_output=True)  # noqa: S603


def test_repair_budget_requires_a_changed_candidate_and_stops_at_three(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    source = tmp_path / "app.py"
    source.write_text("print('first')\n")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "initial")
    ledger = IncidentLedger(tmp_path)
    first = candidate_fingerprint(tmp_path)
    assert ledger.reserve("release-1", first, 3) == 1
    with pytest.raises(PermissionError, match="unchanged"):
        ledger.reserve("release-1", first, 3)
    source.write_text("print('second')\n")
    second = candidate_fingerprint(tmp_path)
    assert second != first
    assert ledger.reserve("release-1", second, 3) == 2
    source.write_text("print('third')\n")
    assert ledger.reserve("release-1", candidate_fingerprint(tmp_path), 3) == 3
    source.write_text("print('fourth')\n")
    with pytest.raises(PermissionError, match="3-attempt limit"):
        ledger.reserve("release-1", candidate_fingerprint(tmp_path), 3)
    assert (tmp_path / ".devops-agent/incidents.json").stat().st_mode & 0o777 == 0o600


def test_untracked_file_changes_fingerprint_and_symlink_is_rejected(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "app.py").write_text("pass\n")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "initial")
    initial = candidate_fingerprint(tmp_path)
    (tmp_path / "incident.log").write_text("first failure\n")
    assert candidate_fingerprint(tmp_path) == initial
    (tmp_path / "incident.log").write_text("second failure\n")
    assert candidate_fingerprint(tmp_path) == initial
    (tmp_path / "new.py").write_text("x = 1\n")
    assert candidate_fingerprint(tmp_path) != initial
    (tmp_path / "new.py").unlink()
    (tmp_path / "new.py").symlink_to("app.py")
    with pytest.raises(ValueError, match="symlink|fingerprint"):
        candidate_fingerprint(tmp_path)


def test_execute_does_not_redeploy_the_same_failed_candidate(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "index.html").write_text("<h1>site</h1>\n")
    _git(tmp_path, "add", "index.html")
    _git(tmp_path, "commit", "-qm", "initial")

    class Provider:
        name = "fixture"

        def plan(self, project, environment):
            return [CommandSpec(["fixture", "deploy"], "deploy", True)]

    class Runner:
        cwd = tmp_path

        def __init__(self):
            self.calls = 0

        def run(self, spec):
            self.calls += 1
            return OperationResult(spec, 1, stderr="build failed")

    provider = Provider()
    status = ProviderStatus("fixture", "fixture", True, "1", True, "test-account")
    plan = make_plan(analyze(tmp_path), load_config(tmp_path), provider, status, "preview")
    runner = Runner()
    assert execute(plan, provider, status, runner, incident="broken-build").verdict == "FAILED"
    with pytest.raises(PermissionError, match="unchanged candidate"):
        execute(plan, provider, status, runner, incident="broken-build")
    assert runner.calls == 1


def test_fingerprint_accepts_monorepo_service_directory(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    service = tmp_path / "apps" / "web"
    service.mkdir(parents=True)
    (service / "index.html").write_text("ok")
    _git(tmp_path, "add", "apps/web/index.html")
    _git(tmp_path, "commit", "-qm", "initial")
    before = candidate_fingerprint(service)
    (service / "index.html").write_text("changed")
    assert candidate_fingerprint(service) != before
