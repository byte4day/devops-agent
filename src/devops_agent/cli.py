"""Machine-readable local command interface for the DevOps skill."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import replace
from pathlib import Path

from devops_agent.core.analyzer import analyze
from devops_agent.core.approvals import ApprovalScope, require_approval
from devops_agent.core.config import load_config
from devops_agent.core.diagnostics import classify
from devops_agent.core.discovery import doctor
from devops_agent.core.executor import AuditLog, execute
from devops_agent.core.planner import choose_provider, make_plan, provider_target
from devops_agent.core.security import SubprocessRunner, redact
from devops_agent.core.verifier import verify
from devops_agent.providers import all_providers
from devops_agent.providers.base import UnsupportedOperation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devops-agent", description="Local support commands for the DevOps Agent skill"
    )
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Project root")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "analyze", "status", "audit"):
        sub.add_parser(name)
    plan = sub.add_parser("plan")
    plan.add_argument("--provider")
    plan.add_argument("--environment", choices=("preview", "staging", "production"))
    plan.add_argument("--dry-run", action="store_true", help="Run read-only provider preflight checks")
    deploy = sub.add_parser("deploy")
    deploy.add_argument("environment", nargs="?", choices=("preview", "staging", "production"))
    deploy.add_argument("--provider")
    deploy.add_argument("--approve", help="Exact target-bound approval phrase from the plan")
    deploy.add_argument("--account", help="Expected active provider account identifier")
    check = sub.add_parser("verify")
    check.add_argument("url")
    check.add_argument("--allow-private", action="store_true")
    diagnose = sub.add_parser("diagnose")
    diagnose.add_argument("--log-file", type=Path)
    diagnose.add_argument("--exit-code", type=int, default=1)
    for name in ("logs", "rollback"):
        action = sub.add_parser(name)
        action.add_argument("--provider", required=True)
        action.add_argument("--release")
        if name == "rollback":
            action.add_argument("--approve")
            action.add_argument("--account")
    ci = sub.add_parser("ci")
    ci.add_argument("action", choices=("status", "diagnose"))
    ci.add_argument("--run-id")
    domain = sub.add_parser("domain")
    domain.add_argument("action", choices=("inspect",))
    domain.add_argument("hostname")
    return parser


def _emit(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


def _run_reads(runner: SubprocessRunner, specs: list[object]) -> list[dict[str, object]]:
    items = []
    for spec in specs:
        if spec.mutating:
            raise PermissionError("Read-only operation returned a mutating command")
        result = runner.run(spec)
        items.append(
            {
                "purpose": spec.purpose,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "diagnostic": classify(result.stderr + "\n" + result.stdout, result.returncode).__dict__
                if not result.ok
                else None,
            }
        )
    return items


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.project.resolve()
    if not root.is_dir():
        _emit({"error": f"Project directory does not exist: {root}"})
        return 2
    try:
        providers = all_providers()
        runner = SubprocessRunner(root)
        if args.command in {"doctor", "status"}:
            _emit(doctor(root, providers))
            return 0
        if args.command == "verify":
            result = verify(args.url, allow_private=args.allow_private)
            _emit(result.to_dict())
            return 0 if result.verdict in {"HEALTHY", "DEGRADED"} else 1
        if args.command == "diagnose":
            if args.log_file:
                if args.log_file.stat().st_size > 1024 * 1024:
                    raise ValueError("Log file exceeds 1 MiB")
                content = args.log_file.read_text(encoding="utf-8", errors="replace")
            else:
                content = sys.stdin.read(1024 * 1024)
            _emit(classify(content, args.exit_code).__dict__)
            return 0
        if args.command == "domain":
            hostname = args.hostname.strip()
            if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", hostname):
                raise ValueError("Invalid hostname")
            result = verify(f"https://{hostname}")
            _emit(result.to_dict())
            return 0 if result.verdict != "FAILED" else 1
        config = load_config(root)
        project = analyze(root)
        if config.project_name:
            project = replace(project, name=config.project_name)
        if args.command == "analyze":
            _emit(project.to_dict())
            return 0
        if args.command == "audit":
            findings = []
            if project.database_dependencies:
                findings.append(
                    "Database dependencies found; review migrations, backups, and secret injection before deployment"
                )
            if not config.health_urls:
                findings.append("No explicit health_urls configured")
            if not (root / ".github/workflows").exists():
                findings.append("No GitHub Actions workflows found")
            if project.services and len(project.services) > 1:
                findings.append("Multiple services found; select a deployable service root")
            _emit(
                {
                    "project": project.name,
                    "scope": "local configuration only",
                    "findings": findings,
                    "remote_security_assessed": False,
                }
            )
            return 0
        if args.command == "ci":
            github = next(p for p in providers if p.name == "github")
            status = github.detect(runner)
            if not status.authenticated:
                raise RuntimeError(status.detail or "gh authentication required")
            target = provider_target(project, config)
            if args.action == "status":
                _emit(_run_reads(runner, github.ci_status(target)))
            else:
                if not args.run_id or not re.fullmatch(r"[1-9][0-9]*", args.run_id):
                    raise ValueError("ci diagnose requires numeric --run-id")
                _emit(_run_reads(runner, github.ci_diagnose(target, args.run_id)))
            return 0
        if args.command in {"logs", "rollback"}:
            provider = next((p for p in providers if p.name == args.provider), None)
            if provider is None:
                raise ValueError(f"Unknown provider {args.provider}")
            status = provider.detect(runner)
            if not status.authenticated:
                raise RuntimeError(status.detail or "Provider authentication required")
            target = provider_target(project, config)
            if args.command == "logs":
                _emit(_run_reads(runner, provider.logs(target, release=args.release)))
                return 0
            require_approval(ApprovalScope("rollback", provider.name, project.name, "production"), args.approve)
            if args.account is not None and status.account != args.account:
                raise PermissionError("Active provider account differs from --account")
            specs = provider.rollback(target, release=args.release)
            audit = AuditLog(root)
            results = []
            for spec in specs:
                if not spec.mutating:
                    raise UnsupportedOperation("Rollback adapter returned a read-only command")
                audit.write(
                    "rollback_start",
                    provider=provider.name,
                    project=project.name,
                    environment="production",
                    executable=Path(spec.argv[0]).name,
                )
                result = runner.run(spec)
                audit.write(
                    "rollback_end",
                    provider=provider.name,
                    project=project.name,
                    returncode=result.returncode,
                )
                results.append(
                    {
                        "purpose": spec.purpose,
                        "returncode": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "diagnostic": classify(result.stderr + "\n" + result.stdout, result.returncode).__dict__
                        if not result.ok
                        else None,
                    }
                )
                if not result.ok:
                    break
            _emit(
                {
                    "provider": provider.name,
                    "release": args.release,
                    "operations": results,
                    "verdict": "FAILED" if any(item["returncode"] for item in results) else "UNVERIFIED",
                    "note": "Run verify against the production URL after rollback",
                }
            )
            return 1 if any(item["returncode"] for item in results) else 0
        statuses = [provider.detect(runner) for provider in providers]
        selected = choose_provider(project, config, statuses, args.provider)
        provider = next(p for p in providers if p.name == selected)
        status = next(s for s in statuses if s.name == selected)
        environment = (args.environment if args.command == "plan" else args.environment) or config.default_environment
        plan = make_plan(project, config, provider, status, environment)
        if args.command == "plan":
            data = plan.to_dict()
            if args.dry_run:
                data["preflight_results"] = _run_reads(runner, list(plan.preflight))
                output_ready = bool(project.output_dir and (root / project.output_dir).is_dir())
                if hasattr(provider, "dry_run") and output_ready:
                    data["native_dry_run_results"] = _run_reads(
                        runner, list(provider.dry_run(provider_target(project, config), environment))
                    )
                elif hasattr(provider, "dry_run"):
                    data["native_dry_run_results"] = "Not run: build output is absent"
                else:
                    data["native_dry_run_results"] = "Provider has no supported native dry-run"
            _emit(data)
            return 0
        fresh = provider.detect(runner)
        report = execute(plan, provider, fresh, runner, approve=args.approve, expected_account=args.account)
        _emit(report.to_dict())
        return 0 if report.verdict in {"HEALTHY", "DEGRADED"} else 1
    except (ValueError, PermissionError, RuntimeError, OSError, KeyError) as exc:
        _emit({"error": redact(str(exc)), "type": type(exc).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
