from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from devops_agent.cli import main
from devops_agent.integrations import relay as relay_cli

FAKE = """#!/usr/bin/env python3
import sys
data = sys.stdin.read() if not sys.stdin.isatty() else ""
blob = data + " ".join(sys.argv)
print("WORKER OK")
print("guardrails=" + str("DevOps Agent worker rules" in blob))
print("task=" + str("CHECK-LOGS" in blob))
print("token=ghp_abcdefghijklmnopqrstuvwxyz0123456789")
"""


@pytest.fixture
def fake_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "fake-agent"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    for name in ("CLAUDE", "CODEX", "CURSOR", "GEMINI", "ANTIGRAVITY", "OPENCODE"):
        monkeypatch.setenv(f"NUMMODE_RELAY_{name}_BIN", str(fake))
    monkeypatch.delenv("NUMMODE_RELAY_DEPTH", raising=False)
    monkeypatch.delenv("NUMMODE_RELAY_DIR", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    return project


def test_build_args_defaults_to_read_only_with_guardrails(tmp_path: Path) -> None:
    args = relay_cli.build_args(["dispatch", "--to", "codex", "--task", "why does CI fail"], tmp_path)
    assert args[args.index("--mode") + 1] == "read"
    task = args[args.index("--task") + 1]
    assert task.startswith("DevOps Agent worker rules")
    assert "why does CI fail" in task
    assert args[args.index("--dir") + 1] == str(tmp_path / ".devops-agent" / "relay")
    assert args[args.index("--cwd") + 1] == str(tmp_path)


@pytest.mark.parametrize(
    ("extra", "message"),
    [(["--mode", "full"], "refused"), (["--mode", "write"], "--allow-write"), (["--mode=full"], "refused")],
)
def test_unsafe_modes_are_refused(tmp_path: Path, extra: list[str], message: str) -> None:
    with pytest.raises(relay_cli.RelayRefused, match=message):
        relay_cli.build_args(["dispatch", "--to", "codex", "--task", "x", *extra], tmp_path)


def test_write_mode_with_explicit_allow(tmp_path: Path) -> None:
    argv = ["dispatch", "--to", "claude", "--task", "x", "--mode", "write", "--allow-write"]
    args = relay_cli.build_args(argv, tmp_path)
    assert "--allow-write" not in args
    assert args[args.index("--mode") + 1] == "write"


def test_dispatch_wait_and_redacted_result(fake_harness: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = fake_harness
    code = main(["--project", str(root), "relay", "dispatch", "--to", "codex", "--task", "CHECK-LOGS", "--wait"])
    assert code == 0
    capsys.readouterr()
    tasks = [p for p in (root / ".devops-agent" / "relay").iterdir() if p.is_dir()]
    assert len(tasks) == 1
    meta = json.loads((tasks[0] / "meta.json").read_text())
    assert (meta["status"], meta["mode"]) == ("done", "read")
    assert main(["--project", str(root), "relay", "result", tasks[0].name]) == 0
    out = capsys.readouterr().out
    assert "guardrails=True" in out and "task=True" in out
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in out


def test_cli_refusal_exit_code(fake_harness: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--project", str(fake_harness), "relay", "dispatch", "--to", "codex", "--task", "x", "--mode", "full"])
    assert code == 2
    assert "refused" in capsys.readouterr().err


def test_help_and_harnesses(fake_harness: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--project", str(fake_harness), "relay"]) == 0
    assert "--allow-write" in capsys.readouterr().out
    assert main(["--project", str(fake_harness), "relay", "harnesses", "--json"]) == 0
    names = {h["name"] for h in json.loads(capsys.readouterr().out)["harnesses"]}
    assert names == {"claude", "codex", "cursor", "gemini", "antigravity", "opencode"}
