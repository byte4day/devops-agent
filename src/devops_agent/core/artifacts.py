"""Guard static upload directories against accidental credential publication."""

from __future__ import annotations

import re
from pathlib import Path

_SENSITIVE = re.compile(
    r"(?i)^(?:\.env(?:\..*)?|\.git|\.aws|\.ssh|credentials(?:\..*)?|id_(?:rsa|ed25519)|"
    r".*\.(?:pem|key|p12|pfx|tfstate))$"
)


def validate_artifact_tree(root: Path, relative: str) -> dict[str, int]:
    """Validate the observed artifact tree before a static provider upload."""
    root = root.resolve()
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts or candidate in {Path("."), Path("./")}:
        raise ValueError("Static artifact output must be a dedicated relative directory")
    if ".." in candidate.parts:
        raise ValueError("Static artifact output cannot escape the project")
    if any(_SENSITIVE.fullmatch(part) for part in candidate.parts):
        raise ValueError("Static artifact output directory has a sensitive name")
    current = root
    for part in candidate.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Static artifact directory cannot contain symbolic links")
    if not current.is_dir() or not current.resolve().is_relative_to(root):
        raise ValueError("Static artifact output directory is absent or outside the project")
    files = 0
    size = 0
    for item in current.rglob("*"):
        relative_item = item.relative_to(current)
        if item.is_symlink():
            raise ValueError(f"Static artifact contains a symbolic link: {relative_item}")
        if any(_SENSITIVE.fullmatch(part) for part in relative_item.parts):
            raise ValueError(f"Static artifact contains a sensitive filename: {relative_item}")
        if item.is_file():
            files += 1
            size += item.stat().st_size
            if files > 10000 or size > 512 * 1024 * 1024:
                raise ValueError("Static artifact exceeds 10,000 files or 512 MiB")
    if not files:
        raise ValueError("Static artifact output directory is empty")
    return {"files": files, "bytes": size}
