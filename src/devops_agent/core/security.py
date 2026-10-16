"""Process isolation, bounded execution, and output redaction."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from devops_agent.providers.base import CommandSpec, OperationResult

_REDACTIONS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s\"']+"),
    re.compile(
        r"(?i)((?:[A-Z0-9_]*?(?:SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY)[A-Z0-9_]*|api[_-]?key|access[_-]?token)\s*[:=]\s*)[^\s,;\"']+"
    ),
    re.compile(r"(?i)(https?://[^:/\s]+:)[^@/\s]+(@)"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{10,})?\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
]


def redact(value: str) -> str:
    """Remove common token forms from logs and reports."""
    for pattern in _REDACTIONS:
        if pattern.groups == 1:
            value = pattern.sub(r"\1[REDACTED]", value)
        elif pattern.groups == 2:
            value = pattern.sub(r"\1[REDACTED]\2", value)
        else:
            value = pattern.sub("[REDACTED]", value)
    return value


class SubprocessRunner:
    """Runs argument vectors without a shell and bounds output/time."""

    def __init__(self, cwd: Path, *, max_output: int = 65536):
        self.cwd = cwd.resolve()
        self.max_output = max_output

    def run(self, spec: CommandSpec) -> OperationResult:
        if not spec.argv or any(not isinstance(arg, str) or "\x00" in arg for arg in spec.argv):
            raise ValueError("Command must be a nonempty string argument vector")
        if spec.timeout < 1 or spec.timeout > 1800:
            raise ValueError("Command timeout must be between 1 and 1800 seconds")
        # No shell, privilege escalation, or stdin-based prompts in autonomous runs.
        if Path(spec.argv[0]).name.lower() in {
            "sudo",
            "su",
            "doas",
            "sh",
            "bash",
            "zsh",
            "cmd",
            "powershell",
        }:
            raise ValueError("Shell and privilege escalation commands are forbidden")
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONSTARTUP", "PYTHONPATH"}}
        env["CI"] = "true"
        try:
            proc = subprocess.run(  # noqa: S603 - argument vector, no shell, validated executable
                spec.argv,
                cwd=(spec.cwd or self.cwd).resolve(),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=spec.timeout,
                check=False,
                shell=False,
            )
            return OperationResult(
                spec,
                proc.returncode,
                redact(proc.stdout[: self.max_output]),
                redact(proc.stderr[: self.max_output]),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            )
            return OperationResult(
                spec,
                124,
                redact(stdout[: self.max_output]),
                redact((stderr + "\n" + str(exc))[: self.max_output]),
                "DEPLOYMENT_TIMEOUT",
            )
        except OSError as exc:
            return OperationResult(spec, 127, "", redact(str(exc)), "DEPENDENCY_FAILURE")
