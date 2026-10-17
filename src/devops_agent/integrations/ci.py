"""Local GitHub Actions inspection, conservative setup, and failure triage.

This module never invokes GitHub or executes repository content. Generated jobs
use fixed action revisions and only commands declared in local project metadata.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

from devops_agent.core.diagnostics import classify
from devops_agent.core.security import redact

_WORKFLOW = "devops-agent-ci.yml"
_CHECKOUT = "d23441a48e516b6c34aea4fa41551a30e30af803"  # actions/checkout v6
_SETUP_PYTHON = "ece7cb06caefa5fff74198d8649806c4678c61a1"  # actions/setup-python v6
_SETUP_NODE = "949feb2413d6458794dcd2491c4babbbce0c15c1"  # actions/setup-node v7
_MAX_FILE = 1024 * 1024
_CI_JOB = re.compile(r"(?:^|[-_])(test|lint|check|build|ci)(?:$|[-_])", re.I)
_EVENT = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


def _small_text(path: Path) -> str:
    if path.is_symlink():
        raise ValueError(f"Refusing symbolic link: {path}")
    if path.stat().st_size > _MAX_FILE:
        raise ValueError(f"File exceeds 1 MiB: {path}")
    return path.read_text(encoding="utf-8")


def _workflow_summary(path: Path, root: Path) -> dict[str, object]:
    summary: dict[str, object] = {"path": str(path.relative_to(root)), "valid": False}
    try:
        data = yaml.safe_load(_small_text(path))
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
            raise ValueError("workflow requires a jobs mapping")
        if "on" not in data and True not in data:
            raise ValueError("workflow requires an on trigger")
        trigger = data.get("on", data.get(True))
        jobs = data["jobs"]
        if not jobs:
            raise ValueError("workflow requires at least one job")
        if any(not isinstance(job, dict) or ("runs-on" not in job and "uses" not in job) for job in jobs.values()):
            raise ValueError("each workflow job requires runs-on or uses")
        events = list(trigger) if isinstance(trigger, (dict, list)) else [trigger]
        safe_events = [event for event in events if isinstance(event, str) and _EVENT.fullmatch(event)]
        summary.update(
            valid=True,
            triggers=safe_events,
            job_count=len(jobs),
            has_ci_job=any(_CI_JOB.search(str(job)) for job in jobs),
        )
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        summary["error"] = redact(str(exc).splitlines()[0])[:300]
    return summary


def inspect_workflows(root: Path) -> dict[str, object]:
    """Summarize local workflow structure without exposing env or secret values."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Project root does not exist: {root}")
    github = root / ".github"
    folder = github / "workflows"
    if github.is_symlink() or folder.is_symlink():
        raise ValueError("GitHub workflow directory cannot be a symbolic link")
    if not folder.exists():
        return {"workflows": [], "has_ci_job": False, "valid": True}
    if not folder.is_dir():
        raise ValueError(".github/workflows is not a directory")
    paths = sorted(path for path in folder.iterdir() if path.suffix.lower() in {".yml", ".yaml"})
    summaries = [_workflow_summary(path, root) for path in paths]
    return {
        "workflows": summaries,
        "has_ci_job": any(item.get("has_ci_job", False) for item in summaries),
        "valid": all(item["valid"] for item in summaries),
    }


def _python_version(root: Path, project: dict[str, Any]) -> str:
    requires = project.get("requires-python")
    if isinstance(requires, str):
        match = re.search(r">=\s*3\.(\d+)\b", requires)
        if match:
            minor = max(11, int(match.group(1)))
            if minor > 14:
                raise ValueError("Project requires a Python version newer than this CI template supports")
            return f"3.{minor}"
    version_file = root / ".python-version"
    if version_file.is_file():
        version = _small_text(version_file).strip()
        if re.fullmatch(r"3\.(?:11|12|13|14)(?:\.\d+)?", version):
            return version
        raise ValueError(".python-version must name a supported Python 3.11–3.14 release")
    return "3.11"


def _python_workflow(root: Path) -> tuple[str, list[str]]:
    manifest = root / "pyproject.toml"
    requirements = root / "requirements.txt"
    if not manifest.is_file() and not requirements.is_file():
        raise ValueError("Python CI requires pyproject.toml or requirements.txt")
    has_tests = (root / "tests").is_dir() or any(root.glob("test_*.py"))
    if not has_tests:
        raise ValueError("Python CI setup requires an existing tests directory or test_*.py file")
    project: dict[str, Any] = {}
    if manifest.is_file():
        try:
            data = tomllib.loads(_small_text(manifest))
        except (tomllib.TOMLDecodeError, UnicodeError) as exc:
            raise ValueError(f"Invalid pyproject.toml: {exc}") from exc
        project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
    version = _python_version(root, project)
    install = (
        "python -m pip install -e . pytest"
        if manifest.is_file()
        else "python -m pip install -r requirements.txt pytest"
    )
    content = f"""# Generated by DevOps Agent. Review project-specific test dependencies before merging.
name: CI
on: [push, pull_request]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{_CHECKOUT} # v6
      - uses: actions/setup-python@{_SETUP_PYTHON} # v6
        with:
          python-version: '{version}'
      - name: Install dependencies
        run: {install}
      - name: Run tests
        run: python -m pytest -q
"""
    return content, [install, "python -m pytest -q"]


def _node_workflow(root: Path) -> tuple[str, list[str]]:
    package_file = root / "package.json"
    if not package_file.is_file():
        raise ValueError("Node CI requires package.json")
    try:
        package = json.loads(_small_text(package_file))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"Invalid package.json: {exc}") from exc
    if not isinstance(package, dict):
        raise ValueError("package.json must be an object")
    manager = package.get("packageManager")
    if isinstance(manager, str) and manager and not manager.startswith("npm@"):
        raise ValueError("Automatic Node CI setup currently supports npm projects only")
    if not (root / "package-lock.json").is_file() and not (root / "npm-shrinkwrap.json").is_file():
        raise ValueError("Node CI requires package-lock.json or npm-shrinkwrap.json for npm ci")
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict):
        raise ValueError("package.json scripts must be an object")
    commands = [
        f"npm run {name}"
        for name in ("lint", "test", "build")
        if isinstance(scripts.get(name), str) and scripts[name].strip()
    ]
    if not commands:
        raise ValueError("Node CI setup requires at least one declared lint, test, or build script")
    version = "22"
    version_file = root / ".nvmrc"
    if version_file.is_file():
        value = _small_text(version_file).strip().lstrip("v")
        if not re.fullmatch(r"(?:20|22|24)(?:\.\d+){0,2}", value):
            raise ValueError(".nvmrc must pin a supported Node 20, 22, or 24 version")
        version = value
    else:
        engines = package.get("engines", {})
        engine_node = engines.get("node") if isinstance(engines, dict) else None
        if isinstance(engine_node, str):
            match = re.fullmatch(r"v?(20|22|24)(?:\.\d+){0,2}", engine_node.strip())
            if match:
                version = engine_node.strip().lstrip("v")
    step_names = {"lint": "Lint", "test": "Test", "build": "Build"}
    steps = "".join(
        f"      - name: {step_names[command.split()[-1]]}\n        run: {command}\n" for command in commands
    )
    content = f"""# Generated by DevOps Agent. Review project-specific Node version before merging.
name: CI
on: [push, pull_request]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{_CHECKOUT} # v6
      - uses: actions/setup-node@{_SETUP_NODE} # v7
        with:
          node-version: '{version}'
          cache: npm
      - name: Install dependencies
        run: npm ci
{steps}"""
    return content, ["npm ci", *commands]


def setup_ci(root: Path, *, runtime: str | None = None) -> dict[str, object]:
    """Create a new CI workflow only when project scripts and existing CI are clear."""
    root = root.resolve()
    inspection = inspect_workflows(root)
    if not inspection["valid"]:
        raise ValueError("Existing workflow YAML is invalid; inspect it before adding CI")
    if inspection["has_ci_job"]:
        return {
            "created": False,
            "reason": "Existing CI job detected; inspect it instead of adding a duplicate",
            **inspection,
        }
    has_python = (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file()
    has_node = (root / "package.json").is_file()
    if runtime is None:
        if has_python == has_node:
            raise ValueError("Choose runtime='python' or runtime='node' for an ambiguous project")
        runtime = "python" if has_python else "node"
    if runtime not in {"python", "node"}:
        raise ValueError("runtime must be python or node")
    content, commands = _python_workflow(root) if runtime == "python" else _node_workflow(root)
    parsed = yaml.safe_load(content)
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("jobs"), dict)
        or ("on" not in parsed and True not in parsed)
    ):
        raise ValueError("Generated workflow failed syntax validation")
    github = root / ".github"
    folder = github / "workflows"
    if github.is_symlink() or folder.is_symlink():
        raise ValueError("GitHub workflow directory cannot be a symbolic link")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / _WORKFLOW
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags, 0o644)
    except FileExistsError as exc:
        raise FileExistsError(f"Refusing to overwrite existing workflow: {target}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {
        "created": True,
        "path": str(target),
        "runtime": runtime,
        "commands": commands,
        "existing_workflows": inspection["workflows"],
    }


_NEXT_CHECK = {
    "DEPENDENCY_FAILURE": "Compare the install step with the lockfile and reproduce the install locally.",
    "BUILD_FAILURE": "Run the failing command locally and fix the reported source or configuration error.",
    "MISSING_ENVIRONMENT_VARIABLE": (
        "Identify the variable name and configure an environment-scoped secret or variable; "
        "never put its value in the workflow."
    ),
    "AUTHENTICATION_FAILURE": ("Check the approved identity method and environment; prefer OIDC where supported."),
    "PERMISSION_DENIED": (
        "Compare the failed operation with token and cloud role permissions; request only the missing scope."
    ),
    "CI_PIPELINE_ERROR": "Inspect the failed job and step in the workflow, then reproduce that step locally.",
    "DEPLOYMENT_TIMEOUT": "Check the timed-out step and external service status before changing the timeout.",
}


def repair_ci(root: Path, log_text: str, exit_code: int = 1) -> dict[str, object]:
    """Classify failed CI evidence and recommend a bounded, human-reviewed fix."""
    if len(log_text) > _MAX_FILE:
        raise ValueError("CI log evidence exceeds 1 MiB")
    inspection = inspect_workflows(root)
    invalid = [item for item in inspection["workflows"] if not item["valid"]]
    if invalid:
        return {
            "category": "CI_PIPELINE_ERROR",
            "hypothesis": "Workflow YAML is invalid",
            "evidence": invalid[0].get("error", "YAML parse error"),
            "next_check": "Fix the reported workflow syntax and validate it before rerunning CI.",
            "changes_applied": False,
            "workflows": inspection["workflows"],
        }
    diagnostic = classify(log_text, exit_code)
    return {
        "category": diagnostic.category,
        "hypothesis": diagnostic.hypothesis,
        "evidence": diagnostic.evidence,
        "next_check": _NEXT_CHECK.get(
            diagnostic.category, "Inspect the first failing step and reproduce it locally before editing the workflow."
        ),
        "changes_applied": False,
        "workflows": inspection["workflows"],
    }
