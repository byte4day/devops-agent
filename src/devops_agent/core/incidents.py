"""Local repair budget that prevents repeat deployment of an unchanged candidate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from devops_agent.core.executor import AuditLog


def candidate_fingerprint(root: Path) -> str:
    """Hash Git's current candidate, including changed and untracked source files.

    This digest is never printed with source content. Git's ignored files (including
    local credentials and build artifacts) do not affect the candidate identity.
    """
    root = root.resolve()
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("Git is required for repair incident tracking")
    command = subprocess.run(  # noqa: S603 - fixed Git command, no shell
        [git, "rev-parse", "--show-toplevel"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if command.returncode != 0 or not root.is_relative_to(Path(command.stdout.strip()).resolve()):
        raise ValueError("Repair incident tracking requires a project inside a Git repository")
    digest = hashlib.sha256()
    ignored_untracked = {".log", ".jsonl", ".tmp", ".sqlite", ".db"}
    for argv in (
        [git, "rev-parse", "HEAD"],
        [git, "diff", "--binary", "HEAD", "--", "."],
        [git, "ls-files", "--others", "--exclude-standard", "-z"],
    ):
        result = subprocess.run(argv, cwd=root, capture_output=True, timeout=30, check=False)  # noqa: S603
        if result.returncode != 0:
            raise RuntimeError("Could not fingerprint the Git candidate")
        if argv[1] != "ls-files":
            digest.update(result.stdout)
        else:
            for raw in result.stdout.split(b"\0"):
                if not raw:
                    continue
                relative = raw.decode("utf-8", errors="surrogateescape")
                if Path(relative).parts[0] == ".devops-agent":
                    continue
                if Path(relative).suffix.lower() in ignored_untracked:
                    continue
                path = root / relative
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
                    raise ValueError(f"Cannot safely fingerprint untracked file {relative!r}")
                digest.update(raw)
                with path.open("rb") as handle:
                    while chunk := handle.read(65536):
                        digest.update(chunk)
    return digest.hexdigest()


class IncidentLedger:
    """Reserve at most a configured number of distinct candidates per incident."""

    def __init__(self, root: Path):
        self.audit = AuditLog(root)
        self.path = self.audit.folder / "incidents.json"
        if self.path.is_symlink():
            raise ValueError("Incident ledger cannot be a symlink")

    def attempts(self, incident: str) -> int:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", incident):
            raise ValueError("Incident ID must be 1-80 safe characters")
        if not self.path.exists():
            return 0
        if self.path.stat().st_size > 65536:
            raise ValueError("Incident ledger exceeds 64 KiB")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get(incident, []), list):
            raise ValueError("Incident ledger is invalid")
        return len(data.get(incident, []))

    def reserve(self, incident: str, fingerprint: str, maximum: int) -> int:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", incident):
            raise ValueError("Incident ID must be 1-80 safe characters")
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("Invalid candidate fingerprint")
        if not 0 <= maximum <= 3:
            raise ValueError("Repair attempt maximum must be 0-3")
        if self.path.exists():
            if self.path.stat().st_size > 65536:
                raise ValueError("Incident ledger exceeds 64 KiB")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Incident ledger is invalid")
        else:
            data = {}
        attempts = data.get(incident, [])
        if not isinstance(attempts, list) or any(not isinstance(item, str) for item in attempts):
            raise ValueError("Incident ledger is invalid")
        if fingerprint in attempts:
            raise PermissionError("This unchanged candidate was already deployed for the incident")
        if len(attempts) >= maximum:
            raise PermissionError(f"Repair incident {incident!r} reached its {maximum}-attempt limit")
        attempts.append(fingerprint)
        data[incident] = attempts
        temporary = self.audit.folder / f".incidents-{os.getpid()}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.audit.write("repair_attempt_reserved", incident=incident, attempt=len(attempts))
        return len(attempts)
