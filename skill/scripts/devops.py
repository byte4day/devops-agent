#!/usr/bin/env python3
"""Run the packaged DevOps Agent CLI from an installed skill."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    skill_dir = Path(__file__).resolve().parent.parent
    metadata = skill_dir / ".devops-agent-install.json"
    executable: str | None = None
    if metadata.is_file():
        try:
            data = json.loads(metadata.read_text(encoding="utf-8"))
            executable = data.get("executable")
        except (OSError, ValueError):
            pass
    executable = os.environ.get("DEVOPS_AGENT_BIN") or executable or shutil.which("devops-agent")
    if not executable or not Path(executable).is_file():
        print(
            "DevOps Agent runtime is unavailable. Re-run installers/install.py --update "
            "for this project or install the package with python -m pip install .",
            file=sys.stderr,
        )
        return 2
    try:
        return subprocess.run(  # noqa: S603
            [executable, *sys.argv[1:]], check=False
        ).returncode
    except OSError as exc:
        print(f"Cannot start DevOps Agent: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
