#!/usr/bin/env python3
"""Install the DevOps Agent skill and its isolated Python runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_SOURCE = REPO_ROOT / "skill"
MARKER = ".devops-agent-install.json"
PLATFORMS = ("codex", "claude", "cursor", "antigravity")


def _home() -> Path:
    return Path.home()


def _data_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", _home() / "AppData" / "Local")) / "DevOpsAgent"
    return Path(os.environ.get("XDG_DATA_HOME", _home() / ".local" / "share")) / "devops-agent"


def _scope_id(project: Path | None) -> str:
    if project is None:
        return "global"
    digest = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:16]
    return f"project-{digest}"


def _runtime_dir(project: Path | None) -> Path:
    return _data_root() / "runtimes" / _scope_id(project)


def _executable(runtime: Path) -> Path:
    return runtime / ("Scripts/devops-agent.exe" if os.name == "nt" else "bin/devops-agent")


def _paths(project: Path | None) -> dict[str, Path]:
    base = project if project is not None else _home()
    return {
        "codex": base / ".agents" / "skills" / "devops",
        "claude": base / ".claude" / "skills" / "devops",
        "cursor": base / ".cursor" / "skills" / "devops",
        "antigravity": (base / ".agents" / "skills" / "devops")
        if project is not None
        else (base / ".gemini" / "config" / "skills" / "devops"),
    }


def _antigravity_cli_path(project: Path | None) -> Path | None:
    if project is not None:
        return None  # Project discovery is shared with Antigravity App, CLI, and IDE.
    return _home() / ".gemini" / "antigravity-cli" / "skills" / "devops"


def _assert_scoped_target(path: Path, project: Path | None) -> None:
    base = (project if project is not None else _home()).resolve()
    if not path.parent.resolve().is_relative_to(base):
        raise RuntimeError(f"Skill path escapes installation scope through a symlink: {path}")


def _read_marker(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads((path / MARKER).read_text(encoding="utf-8"))
        return data if data.get("managed_by") == "devops-agent-installer" else None
    except (OSError, ValueError, AttributeError):
        return None


def _validate_skill() -> None:
    manifest = SKILL_SOURCE / "SKILL.md"
    if not manifest.is_file():
        raise RuntimeError(f"Missing {manifest}")
    body = manifest.read_text(encoding="utf-8")
    if not body.startswith("---\n"):
        raise RuntimeError("SKILL.md requires YAML frontmatter")
    sections = body.split("---\n", 2)
    if len(sections) < 3:
        raise RuntimeError("SKILL.md frontmatter is not closed")
    fields = dict(line.split(":", 1) for line in sections[1].splitlines() if ":" in line and not line.startswith(" "))
    if fields.get("name", "").strip() != "devops" or not fields.get("description", "").strip():
        raise RuntimeError("SKILL.md must declare name: devops and a description")
    if not (SKILL_SOURCE / "scripts" / "devops.py").is_file():
        raise RuntimeError("Missing skill/scripts/devops.py")


def _run_checked(argv: list[str], timeout: int = 300) -> None:
    # The caller constructs a fixed argument vector; no shell interpretation occurs.
    process = subprocess.run(  # noqa: S603
        argv, check=False, capture_output=True, text=True, timeout=timeout
    )
    if process.returncode:
        detail = (process.stderr or process.stdout).strip()[-1500:]
        raise RuntimeError(f"Command failed ({process.returncode}): {argv[0]}: {detail}")


def _backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.backup-{stamp}")
    index = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = path.with_name(f"{path.name}.backup-{stamp}-{index}")
        index += 1
    return candidate


def _install_runtime(project: Path | None, update: bool) -> Path:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required")
    runtime = _runtime_dir(project)
    if runtime.is_symlink():
        raise RuntimeError(f"Refusing to use symlinked runtime: {runtime}")
    if runtime.exists() and _read_marker(runtime) is None:
        raise RuntimeError(f"Refusing to replace unrecognized runtime: {runtime}")
    if _executable(runtime).is_file() and not update:
        return runtime
    runtime.parent.mkdir(parents=True, exist_ok=True)
    # Virtual environments embed their absolute path in console-script shebangs.
    # Build at the final path and restore the previous runtime if installation fails.
    backup = _backup_path(runtime) if runtime.exists() else None
    if backup is not None:
        runtime.rename(backup)
    try:
        venv.EnvBuilder(with_pip=True).create(runtime)
        pip = runtime / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
        _run_checked([str(pip), "install", "--disable-pip-version-check", str(REPO_ROOT)], timeout=600)
        _run_checked([str(_executable(runtime)), "--help"], timeout=30)
        (runtime / MARKER).write_text(
            json.dumps({"managed_by": "devops-agent-installer", "source": str(REPO_ROOT)}) + "\n",
            encoding="utf-8",
        )
        return runtime
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        if runtime.exists():
            shutil.rmtree(runtime)
        if backup is not None:
            backup.rename(runtime)
        raise


def _install_skill(target: Path, platforms: list[str], project: Path | None, runtime: Path, update: bool) -> str:
    existing = target.exists() or target.is_symlink()
    existing_marker = _read_marker(target) if existing else None
    if existing and (target.is_symlink() or existing_marker is None):
        raise RuntimeError(f"Refusing to replace unmanaged skill: {target}")
    if existing and not update:
        return f"Already installed: {target} (use --update to refresh)"
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".devops-skill-", dir=target.parent))
    backup: Path | None = None
    try:
        shutil.copytree(SKILL_SOURCE, staging, dirs_exist_ok=True, symlinks=False)
        marker = {
            "managed_by": "devops-agent-installer",
            "format": 1,
            "source": str(REPO_ROOT),
            "scope": "project" if project is not None else "global",
            "platforms": sorted(set(platforms) | set(existing_marker.get("platforms", [])))
            if existing_marker
            else platforms,
            "runtime": str(runtime),
            "executable": str(_executable(runtime)),
        }
        (staging / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        if existing:
            backup = _backup_path(target)
            target.rename(backup)
        staging.rename(target)
        return f"Installed: {target}"
    except OSError:
        if backup is not None and backup.exists() and not target.exists():
            backup.rename(target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _selection(args: argparse.Namespace) -> list[str]:
    if args.all or not args.platform:
        return list(PLATFORMS)
    return [args.platform]


def _targets(selected: list[str], project: Path | None, *, install: bool) -> dict[Path, list[str]]:
    paths = _paths(project)
    result: dict[Path, list[str]] = {}
    for platform in selected:
        path = paths[platform]
        if install and platform == "cursor" and "codex" not in selected:
            shared = paths["codex"]
            if _read_marker(shared):
                print(f"Cursor already discovers shared skill at {shared}; skipping duplicate")
                continue
        if install and platform == "cursor" and "codex" in selected:
            path = paths["codex"]
        result.setdefault(path, []).append(platform)
    antigravity_cli = _antigravity_cli_path(project)
    if "antigravity" in selected and antigravity_cli is not None:
        result[antigravity_cli] = ["antigravity"]
    return result


def _status(project: Path | None, selected: list[str]) -> int:
    paths = _paths(project)
    for platform in selected:
        native = paths[platform]
        shared = paths["codex"] if platform == "cursor" else native
        target = native if _read_marker(native) else shared
        marker = _read_marker(target)
        if marker:
            executable = Path(str(marker.get("executable", "")))
            health = "ready" if executable.is_file() else "runtime missing"
            print(f"{platform}: {health} at {target}")
        elif target.exists() or target.is_symlink():
            print(f"{platform}: unmanaged at {target}")
        else:
            print(f"{platform}: not installed")
    antigravity_cli = _antigravity_cli_path(project)
    if "antigravity" in selected and antigravity_cli is not None:
        marker = _read_marker(antigravity_cli)
        if marker:
            executable = Path(str(marker.get("executable", "")))
            health = "ready" if executable.is_file() else "runtime missing"
            print(f"antigravity-cli: {health} at {antigravity_cli}")
        else:
            print("antigravity-cli: not installed")
    return 0


def _consolidate_cursor(project: Path | None) -> None:
    paths = _paths(project)
    shared = paths["codex"]
    native = paths["cursor"]
    if not native.exists():
        return
    if _read_marker(shared) is None or _read_marker(native) is None:
        raise RuntimeError("Cannot consolidate an unmanaged Cursor skill")
    backup = _backup_path(native)
    native.rename(backup)
    marker = _read_marker(shared)
    assert marker is not None
    marker["platforms"] = sorted(set(marker.get("platforms", [])) | {"cursor"})
    (shared / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    print(f"Consolidated Cursor discovery; previous installation backed up at {backup}")


def _uninstall(project: Path | None, selected: list[str]) -> None:
    targets = _targets(selected, project, install=False)
    all_paths = _paths(project)
    if len(selected) == len(PLATFORMS):
        targets.setdefault(all_paths["cursor"], ["cursor"])
    for path in targets:
        _assert_scoped_target(path, project)
        marker = _read_marker(path)
        if marker is None:
            if path.exists() or path.is_symlink():
                print(f"Skipped unmanaged skill: {path}")
            continue
        installed_platforms = set(marker.get("platforms", []))
        if not installed_platforms.issubset(set(selected)):
            raise RuntimeError(
                f"Cannot remove shared skill {path}: also used by "
                f"{', '.join(sorted(installed_platforms - set(selected)))}. "
                "Uninstall those platforms together with --all."
            )
        if path.is_symlink():
            raise RuntimeError(f"Refusing to remove symlink: {path}")
        shutil.rmtree(path)
        print(f"Removed: {path}")
    # Keep the shared runtime when any managed installation in this scope remains.
    installed_paths = set(all_paths.values())
    antigravity_cli = _antigravity_cli_path(project)
    if antigravity_cli is not None:
        installed_paths.add(antigravity_cli)
    if any(_read_marker(path) for path in installed_paths):
        return
    runtime = _runtime_dir(project)
    if _read_marker(runtime):
        shutil.rmtree(runtime)
        print(f"Removed runtime: {runtime}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--all", action="store_true", help="All four AI platforms")
    choice.add_argument("--platform", choices=PLATFORMS, help="One AI platform")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--global", dest="global_scope", action="store_true", help="Install for this user")
    scope.add_argument("--project", action="store_true", help="Install in current project (default)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--doctor", action="store_true", help="Check installation status")
    action.add_argument("--uninstall", action="store_true", help="Remove managed installation")
    parser.add_argument("--update", action="store_true", help="Back up and replace managed installations")
    args = parser.parse_args(argv)
    if args.update and (args.doctor or args.uninstall):
        parser.error("--update cannot be used with --doctor or --uninstall")
    project = None if args.global_scope else Path.cwd().resolve()
    selected = _selection(args)
    try:
        if args.doctor:
            return _status(project, selected)
        if args.uninstall:
            _uninstall(project, selected)
            return 0
        _validate_skill()
        targets = _targets(selected, project, install=True)
        if not targets:
            return 0
        for path in targets:
            _assert_scoped_target(path, project)
            if (path.exists() or path.is_symlink()) and (path.is_symlink() or _read_marker(path) is None):
                raise RuntimeError(f"Refusing to replace unmanaged skill: {path}")
        paths = _paths(project)
        if paths["codex"] in targets and paths["cursor"].exists():
            if _read_marker(paths["cursor"]) is None:
                raise RuntimeError("Refusing to duplicate unmanaged Cursor skill")
        runtime = _install_runtime(project, args.update)
        for path, platforms in targets.items():
            print(_install_skill(path, platforms, project, runtime, args.update))
        if paths["codex"] in targets:
            _consolidate_cursor(project)
        print("Invoke as $devops in Codex or /devops in Claude Code or Cursor.")
        print("In Antigravity App or CLI, use /devops; in its IDE, select the devops skill.")
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Installation error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
