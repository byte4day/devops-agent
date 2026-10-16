"""Installer behavior in isolated homes and workspaces."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "devops_installer", Path(__file__).resolve().parents[1] / "installers" / "install.py"
)
assert _SPEC and _SPEC.loader
installer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installer)


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    project = tmp_path / "app"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "data"))
    monkeypatch.setattr(installer, "_home", lambda: home)
    monkeypatch.chdir(project)

    def fake_runtime(scope: Path | None, update: bool) -> Path:
        runtime = installer._runtime_dir(scope)
        runtime.mkdir(parents=True, exist_ok=True)
        binary = installer._executable(runtime)
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/usr/bin/env python3\nprint('mock runtime')\n", encoding="utf-8")
        binary.chmod(0o755)
        (runtime / installer.MARKER).write_text(json.dumps({"managed_by": "devops-agent-installer"}), encoding="utf-8")
        return runtime

    monkeypatch.setattr(installer, "_install_runtime", fake_runtime)
    return home, project


def test_project_all_deduplicates_shared_skill_and_runs_launcher(
    isolated: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _, project = isolated
    assert installer.main(["--all", "--project"]) == 0
    shared = project / ".agents/skills/devops"
    claude = project / ".claude/skills/devops"
    assert shared.joinpath("SKILL.md").is_file()
    assert claude.joinpath("SKILL.md").is_file()
    assert not project.joinpath(".cursor/skills/devops").exists()
    assert installer.main(["--doctor", "--all", "--project"]) == 0
    report = capsys.readouterr().out
    assert all(f"{name}: ready" in report for name in installer.PLATFORMS)
    if sys.platform == "win32":
        return
    process = subprocess.run(  # noqa: S603
        [sys.executable, str(shared / "scripts/devops.py"), "doctor"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0
    assert process.stdout.strip() == "mock runtime"


def test_global_install_and_idempotent_reinstall(isolated: tuple[Path, Path]) -> None:
    home, _ = isolated
    assert installer.main(["--platform", "claude", "--global"]) == 0
    target = home / ".claude/skills/devops"
    assert target.is_dir()
    assert installer.main(["--platform", "claude", "--global"]) == 0
    assert not list(target.parent.glob("devops.backup-*"))
    assert installer.main(["--platform", "claude", "--global", "--update"]) == 0
    assert len(list(target.parent.glob("devops.backup-*"))) == 1


def test_uninstall_cleans_only_managed_paths(isolated: tuple[Path, Path]) -> None:
    _, project = isolated
    assert installer.main(["--all"]) == 0
    assert installer.main(["--uninstall", "--all"]) == 0
    assert not project.joinpath(".agents/skills/devops").exists()
    assert not project.joinpath(".claude/skills/devops").exists()
    assert not installer._runtime_dir(project).exists()


def test_unmanaged_installation_is_preserved(isolated: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _, project = isolated
    target = project / ".agents/skills/devops"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("user file", encoding="utf-8")
    assert installer.main(["--platform", "codex"]) == 1
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "user file"
    assert "Refusing to replace unmanaged" in capsys.readouterr().err


def test_project_parent_symlink_cannot_redirect_installation(
    isolated: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    if sys.platform == "win32":
        pytest.skip("Symlink creation requires Windows developer mode or elevated permissions")
    _, project = isolated
    outside = project.parent / "outside"
    outside.mkdir()
    (project / ".agents").symlink_to(outside, target_is_directory=True)
    assert installer.main(["--platform", "codex"]) == 1
    assert not outside.joinpath("skills/devops").exists()
    assert "escapes installation scope" in capsys.readouterr().err


def test_shared_install_cannot_be_partially_uninstalled(
    isolated: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _, project = isolated
    assert installer.main(["--all"]) == 0
    assert installer.main(["--uninstall", "--platform", "codex"]) == 1
    assert project.joinpath(".agents/skills/devops/SKILL.md").exists()
    assert "shared skill" in capsys.readouterr().err


def test_updating_one_platform_preserves_shared_ownership(isolated: tuple[Path, Path]) -> None:
    _, project = isolated
    assert installer.main(["--all"]) == 0
    assert installer.main(["--platform", "codex", "--update"]) == 0
    marker = json.loads(project.joinpath(".agents/skills/devops", installer.MARKER).read_text(encoding="utf-8"))
    assert set(marker["platforms"]) == {"codex", "cursor", "antigravity"}
    assert installer.main(["--uninstall", "--platform", "codex"]) == 1


def test_single_cursor_uses_native_location(isolated: tuple[Path, Path]) -> None:
    _, project = isolated
    assert installer.main(["--platform", "cursor"]) == 0
    assert project.joinpath(".cursor/skills/devops/SKILL.md").is_file()
    assert not project.joinpath(".agents/skills/devops").exists()


def test_all_install_consolidates_existing_cursor_copy(isolated: tuple[Path, Path]) -> None:
    _, project = isolated
    assert installer.main(["--platform", "cursor"]) == 0
    assert installer.main(["--all"]) == 0
    assert project.joinpath(".agents/skills/devops/SKILL.md").is_file()
    assert not project.joinpath(".cursor/skills/devops").exists()
    assert list(project.joinpath(".cursor/skills").glob("devops.backup-*"))
    marker = json.loads(project.joinpath(".agents/skills/devops", installer.MARKER).read_text(encoding="utf-8"))
    assert "cursor" in marker["platforms"]


def test_single_antigravity_global_uses_google_location(isolated: tuple[Path, Path]) -> None:
    home, _ = isolated
    assert installer.main(["--platform", "antigravity", "--global"]) == 0
    assert home.joinpath(".gemini/config/skills/devops/SKILL.md").is_file()
    assert home.joinpath(".gemini/antigravity-cli/skills/devops/SKILL.md").is_file()


def test_launcher_passes_metacharacters_as_literal_arguments(
    isolated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform == "win32":
        pytest.skip("This subprocess fixture uses a POSIX shebang")
    _, project = isolated
    assert installer.main(["--platform", "codex"]) == 0
    sentinel = project / "should-not-exist"
    runner = project / "print-args.py"
    runner.write_text("#!/usr/bin/env python3\nimport sys\nprint(sys.argv[1])\n", encoding="utf-8")
    runner.chmod(0o755)
    monkeypatch.setenv("DEVOPS_AGENT_BIN", str(runner))
    argument = f"$(touch {sentinel})"
    process = subprocess.run(  # noqa: S603
        [sys.executable, str(project / ".agents/skills/devops/scripts/devops.py"), argument],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0
    assert process.stdout.strip() == argument
    assert not sentinel.exists()
