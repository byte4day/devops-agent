from __future__ import annotations

from pathlib import Path

import pytest

from devops_agent.core.artifacts import validate_artifact_tree


def test_static_artifact_accepts_dedicated_build_output(tmp_path: Path) -> None:
    folder = tmp_path / "dist"
    folder.mkdir()
    (folder / "index.html").write_text("<h1>ready</h1>")
    (folder / "app.css").write_text("body{}")
    assert validate_artifact_tree(tmp_path, "dist")["files"] == 2


@pytest.mark.parametrize("name", [".env", ".env.production", "private.pem", "id_ed25519", "terraform.tfstate"])
def test_static_artifact_rejects_sensitive_filenames(tmp_path: Path, name: str) -> None:
    folder = tmp_path / "dist"
    folder.mkdir()
    (folder / "index.html").write_text("ok")
    (folder / name).write_text("sensitive")
    with pytest.raises(ValueError, match="sensitive filename"):
        validate_artifact_tree(tmp_path, "dist")


def test_static_artifact_rejects_symlink_and_project_root(tmp_path: Path) -> None:
    folder = tmp_path / "dist"
    folder.mkdir()
    (folder / "index.html").write_text("ok")
    (folder / "link").symlink_to("index.html")
    with pytest.raises(ValueError, match="symbolic link"):
        validate_artifact_tree(tmp_path, "dist")
    with pytest.raises(ValueError, match="dedicated"):
        validate_artifact_tree(tmp_path, ".")
