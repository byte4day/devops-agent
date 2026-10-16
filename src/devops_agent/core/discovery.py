"""Read-only local and provider environment discovery."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path

from devops_agent.core.analyzer import analyze
from devops_agent.core.security import SubprocessRunner, redact
from devops_agent.providers.base import CommandSpec


def _git(runner: SubprocessRunner, *args: str) -> str | None:
    result = runner.run(CommandSpec(["git", *args], "inspect git", False, timeout=15))
    return redact(result.stdout.strip()) if result.ok else None


def doctor(root: Path, providers: list[object]) -> dict[str, object]:
    root = root.resolve()
    runner = SubprocessRunner(root)
    statuses = [provider.detect(runner) for provider in providers]
    git_root = _git(runner, "rev-parse", "--show-toplevel") if shutil.which("git") else None
    project = analyze(root)
    return {
        "os": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "cwd": str(root),
        "git": {
            "root": git_root,
            "branch": _git(runner, "branch", "--show-current") if git_root else None,
            "commit": _git(runner, "rev-parse", "HEAD") if git_root else None,
            "dirty": bool(_git(runner, "status", "--porcelain")) if git_root else None,
        },
        "providers": [status.__dict__ for status in statuses],
        "hosting_config": project.hosting_configs,
        "ci_workflows": sorted(str(p.relative_to(root)) for p in (root / ".github/workflows").glob("*.y*ml"))
        if (root / ".github/workflows").is_dir()
        else [],
        "tooling": {
            name: bool(shutil.which(name))
            for name in ("docker", "kubectl", "helm", "terraform", "tofu", "pulumi", "gh")
        },
        "credential_values_inspected": False,
    }
