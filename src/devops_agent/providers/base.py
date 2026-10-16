"""Typed boundary between provider adapters and the command executor.

Adapters construct argument vectors only. They never invoke a shell or make a
cloud mutation while detecting an installation or preparing a plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


class UnsupportedOperation(ValueError):
    """The requested target lacks a verified, safe deployment path."""


@dataclass(frozen=True)
class CommandSpec:
    argv: list[str]
    purpose: str
    mutating: bool
    production: bool = False
    timeout: int = 120
    cwd: Path | None = None

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(arg, str) or not arg for arg in self.argv):
            raise ValueError("command arguments must be nonempty strings")
        if self.timeout < 1:
            raise ValueError("command timeout must be positive")


@dataclass(frozen=True)
class OperationResult:
    command: CommandSpec
    returncode: int
    stdout: str = ""
    stderr: str = ""
    category: str | None = None
    url: str | None = None
    release: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    binary: str
    installed: bool
    version: str | None = None
    authenticated: bool = False
    account: str | None = None
    region: str | None = None
    capabilities: tuple[str, ...] = ()
    detail: str | None = None


class Runner(Protocol):
    def run(self, spec: CommandSpec) -> OperationResult: ...


class BaseProvider:
    name = ""
    binary = ""
    capabilities: tuple[str, ...] = ()
    version_args: tuple[str, ...] = ("--version",)
    login_hint = ""

    def _read(self, runner: Runner, *args: str, timeout: int = 20) -> OperationResult:
        spec = CommandSpec([self.binary, *args], "inspect provider", False, timeout=timeout)
        try:
            return runner.run(spec)
        except (FileNotFoundError, OSError) as exc:
            return OperationResult(spec, 127, stderr=str(exc), category="CLI_UNAVAILABLE")

    def _version(self, runner: Runner) -> str | None:
        result = self._read(runner, *self.version_args)
        if result.ok:
            return (result.stdout or result.stderr).strip().splitlines()[0][:120] or None
        return None

    def _status(
        self,
        version: str | None,
        authenticated: bool,
        *,
        account: str | None = None,
        region: str | None = None,
        detail: str | None = None,
        capabilities: tuple[str, ...] | None = None,
    ) -> ProviderStatus:
        return ProviderStatus(
            self.name,
            self.binary,
            version is not None,
            version,
            authenticated,
            account,
            region,
            self.capabilities if capabilities is None else capabilities,
            detail,
        )

    def logs(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        raise UnsupportedOperation(f"{self.name} log retrieval is unsupported")

    def rollback(self, project: Any, *, release: str | None = None) -> list[CommandSpec]:
        raise UnsupportedOperation(f"{self.name} rollback is unsupported")

    def parse_deployment(self, result: OperationResult) -> OperationResult:
        """Enrich a completed result only with observed provider output."""
        return result


def value(project: Any, key: str, default: Any = None) -> Any:
    if isinstance(project, Mapping):
        return project.get(key, default)
    return getattr(project, key, default)


def root_of(project: Any) -> Path:
    root = value(project, "root", value(project, "path"))
    if root is None:
        raise UnsupportedOperation("project root is required")
    return Path(root).resolve()


def require_static(project: Any, provider: str) -> None:
    if value(project, "static") is not True or value(project, "api_routes", False):
        raise UnsupportedOperation(f"{provider} static deployment requires a static project without API routes")


def output_of(project: Any) -> str:
    output = value(project, "output_dir")
    if (
        not isinstance(output, str)
        or not output.strip()
        or output in {".", "./"}
        or Path(output).is_absolute()
        or ".." in Path(output).parts
    ):
        raise UnsupportedOperation("a safe relative build output directory is required")
    return output


def environment_name(environment: str) -> str:
    if environment not in {"preview", "staging", "production"}:
        raise UnsupportedOperation("environment must be preview, staging, or production")
    return environment
