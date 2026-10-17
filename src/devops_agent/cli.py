"""Machine-readable local command interface for the DevOps skill."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, replace
from pathlib import Path

from devops_agent.core.analyzer import analyze
from devops_agent.core.approvals import ApprovalScope, require_approval
from devops_agent.core.artifacts import validate_artifact_tree
from devops_agent.core.config import load_config
from devops_agent.core.diagnostics import classify
from devops_agent.core.discovery import doctor
from devops_agent.core.executor import AuditLog, execute
from devops_agent.core.incidents import IncidentLedger, candidate_fingerprint
from devops_agent.core.planner import choose_provider, make_plan, provider_target
from devops_agent.core.security import SubprocessRunner, redact
from devops_agent.core.verifier import verify
from devops_agent.integrations.ci import inspect_workflows, repair_ci, setup_ci
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
    deploy.add_argument("--incident", help="Repair incident ID; enforces attempt budget and changed candidate")
    check = sub.add_parser("verify")
    check.add_argument("url")
    check.add_argument("--allow-private", action="store_true")
    diagnose = sub.add_parser("diagnose")
    diagnose.add_argument("--log-file", type=Path)
    diagnose.add_argument("--exit-code", type=int, default=1)
    fix = sub.add_parser("fix", help="Triage an incident and show its bounded repair budget")
    fix.add_argument("--incident", required=True)
    fix.add_argument("--log-file", type=Path)
    fix.add_argument("--exit-code", type=int, default=1)
    for name in ("logs", "rollback"):
        action = sub.add_parser(name)
        action.add_argument("--provider", required=True)
        action.add_argument("--release")
        if name == "rollback":
            action.add_argument("--approve")
            action.add_argument("--account")
    ci = sub.add_parser("ci")
    ci.add_argument("action", choices=("status", "diagnose", "repair", "setup", "inspect"))
    ci.add_argument("--run-id")
    ci.add_argument("--log-file", type=Path)
    ci.add_argument("--exit-code", type=int, default=1)
    ci.add_argument("--runtime", choices=("python", "node"))
    domain = sub.add_parser("domain")
    domain.add_argument("action", choices=("inspect", "plan"))
    domain.add_argument("hostname")
    domain.add_argument("--provider")
    domain.add_argument("--target", help="Existing provider project name for a domain plan")
    infra = sub.add_parser("infra", help="Reviewed AWS CloudFormation update workflow")
    infra.add_argument("action", choices=("validate", "prepare", "review", "apply"))
    infra.add_argument("--change-set-name")
    infra.add_argument("--change-set-arn")
    infra.add_argument("--digest", help="Exact SHA-256 review digest for apply")
    infra.add_argument("--approve")
    infra.add_argument("--approve-destructive")
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


def _reads_failed(items: list[dict[str, object]]) -> bool:
    return any(item["returncode"] != 0 for item in items)


def _run_scoped_ops(
    root: Path, runner: SubprocessRunner, specs: list[object], *, operation: str, target: str
) -> list[dict[str, object]]:
    audit = AuditLog(root)
    outcomes: list[dict[str, object]] = []
    for spec in specs:
        audit.write("operation_start", operation=operation, target=target, purpose=spec.purpose)
        result = runner.run(spec)
        audit.write("operation_end", operation=operation, target=target, returncode=result.returncode)
        outcomes.append(
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
    return outcomes


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
        if args.command in {"diagnose", "fix"}:
            if args.log_file:
                if args.log_file.stat().st_size > 1024 * 1024:
                    raise ValueError("Log file exceeds 1 MiB")
                content = args.log_file.read_text(encoding="utf-8", errors="replace")
            else:
                content = sys.stdin.read(1024 * 1024)
            diagnostic = classify(content, args.exit_code)
            if args.command == "fix":
                config = load_config(root)
                attempts = IncidentLedger(root).attempts(args.incident)
                _emit(
                    {
                        "incident": args.incident,
                        "diagnostic": diagnostic.__dict__,
                        "candidate_fingerprint": candidate_fingerprint(root),
                        "attempts_used": attempts,
                        "attempts_remaining": max(0, config.max_repair_attempts - attempts),
                        "changes_applied": False,
                        "next_step": (
                            "Inspect evidence and source, apply the smallest authorized fix, "
                            "test it, then deploy with --incident"
                        ),
                    }
                )
            else:
                _emit(diagnostic.__dict__)
            return 0
        if args.command == "domain":
            hostname = args.hostname.strip()
            if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", hostname):
                raise ValueError("Invalid hostname")
            if args.action == "inspect":
                result = verify(f"https://{hostname}")
                data: dict[str, object] = {"public_verification": result.to_dict()}
                if args.provider:
                    provider = next((p for p in providers if p.name == args.provider), None)
                    if provider is None:
                        raise ValueError(f"Unknown provider {args.provider}")
                    status = provider.detect(runner)
                    if not status.authenticated:
                        raise RuntimeError(status.detail or "Provider authentication required")
                    if not hasattr(provider, "inspect_domains"):
                        raise UnsupportedOperation(f"{provider.name} domain inspection is unsupported")
                    project = analyze(root)
                    target = provider_target(project, load_config(root))
                    data["provider_inspection"] = _run_reads(runner, provider.inspect_domains(target))
                _emit(data)
                return 1 if result.verdict == "FAILED" or _reads_failed(data.get("provider_inspection", [])) else 0
            if not args.provider or not args.target:
                raise ValueError("domain plan requires --provider and --target")
            provider = next((p for p in providers if p.name == args.provider), None)
            if provider is None:
                raise ValueError(f"Unknown provider {args.provider}")
            if not hasattr(provider, "plan_domain_change"):
                raise UnsupportedOperation(f"{provider.name} domain planning is unsupported")
            project = analyze(root)
            proposal = provider.plan_domain_change(provider_target(project, load_config(root)), hostname, args.target)
            _emit(
                {
                    **proposal,
                    "inspection": [asdict(spec) for spec in proposal["inspection"]],
                    "mutation": [asdict(spec) for spec in proposal["mutation"]],
                    "note": "Proposal only; no DNS or hosting mutation was executed",
                }
            )
            return 0
        if args.command == "infra":
            config = load_config(root)
            project = analyze(root)
            aws = next(p for p in providers if p.name == "aws")
            status = aws.detect(runner)
            if not status.authenticated:
                raise RuntimeError(status.detail or "AWS authentication required")
            if not config.aws_account_id or status.account != config.aws_account_id:
                raise PermissionError("Active AWS account must match devops.yaml aws_account_id")
            target = provider_target(project, config)
            if args.action == "validate":
                results = _run_reads(runner, aws.validate_cloudformation(target))
                _emit(results)
                return 1 if _reads_failed(results) else 0
            scope_target = f"{config.aws_account_id}/{config.aws_region}/{config.cloudformation_stack}"
            if args.action == "prepare":
                if not args.change_set_name:
                    raise ValueError("infra prepare requires --change-set-name")
                specs = aws.cloudformation_prepare(target, runner, args.change_set_name)
                require_approval(ApprovalScope("infra-prepare", "aws", scope_target, "production"), args.approve)
                outcomes = _run_scoped_ops(root, runner, specs, operation="infra_prepare", target=scope_target)
                failed = any(item["returncode"] for item in outcomes)
                _emit({"operations": outcomes, "verdict": "FAILED" if failed else "UNVERIFIED"})
                return 1 if failed else 0
            if not args.change_set_arn:
                raise ValueError(f"infra {args.action} requires --change-set-arn")
            if args.action == "review":
                _emit(asdict(aws.cloudformation_review(target, runner, args.change_set_arn)))
                return 0
            if not args.digest:
                raise ValueError("infra apply requires --digest from a fresh review")
            scoped_digest = f"{scope_target}/{args.digest}"
            require_approval(ApprovalScope("infra-apply", "aws", scoped_digest, "production"), args.approve)
            destructive = args.approve_destructive is not None
            if destructive:
                require_approval(
                    ApprovalScope("infra-destructive", "aws", scoped_digest, "production"),
                    args.approve_destructive,
                )
            specs = aws.cloudformation_execute(
                target, runner, args.change_set_arn, args.digest, allow_destructive=destructive
            )
            outcomes = _run_scoped_ops(root, runner, specs, operation="infra_apply", target=scoped_digest)
            failed = any(item["returncode"] for item in outcomes)
            _emit({"operations": outcomes, "verdict": "FAILED" if failed else "UNVERIFIED"})
            return 1 if failed else 0
        config = load_config(root)
        project = analyze(root)
        if config.project_name:
            project = replace(project, name=config.project_name)
        if config.output_dir:
            project = replace(project, output_dir=config.output_dir)
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
            if args.action == "inspect":
                inspection = inspect_workflows(root)
                _emit(inspection)
                return 0 if inspection["valid"] else 1
            if args.action == "setup":
                _emit(setup_ci(root, runtime=args.runtime))
                return 0
            github = next(p for p in providers if p.name == "github")
            if args.action == "repair" and not args.run_id:
                if args.log_file:
                    if args.log_file.stat().st_size > 1024 * 1024:
                        raise ValueError("Log file exceeds 1 MiB")
                    content = args.log_file.read_text(encoding="utf-8", errors="replace")
                else:
                    content = sys.stdin.read(1024 * 1024)
                _emit(repair_ci(root, content, args.exit_code))
                return 0
            status = github.detect(runner)
            if not status.authenticated:
                raise RuntimeError(status.detail or "gh authentication required")
            target = provider_target(project, config)
            if args.action == "status":
                results = _run_reads(runner, github.ci_status(target))
                _emit(results)
            else:
                if not args.run_id or not re.fullmatch(r"[1-9][0-9]*", args.run_id):
                    raise ValueError("ci diagnose requires numeric --run-id")
                results = _run_reads(runner, github.ci_diagnose(target, args.run_id))
                if args.action == "repair":
                    evidence = "\n".join(str(item["stdout"]) + "\n" + str(item["stderr"]) for item in results)
                    _emit({"run": results, "repair": repair_ci(root, evidence, args.exit_code)})
                else:
                    _emit(results)
            return 1 if _reads_failed(results) else 0
        if args.command in {"logs", "rollback"}:
            provider = next((p for p in providers if p.name == args.provider), None)
            if provider is None:
                raise ValueError(f"Unknown provider {args.provider}")
            status = provider.detect(runner)
            if not status.authenticated:
                raise RuntimeError(status.detail or "Provider authentication required")
            target = provider_target(project, config)
            if args.command == "logs":
                results = _run_reads(runner, provider.logs(target, release=args.release))
                _emit(results)
                return 1 if _reads_failed(results) else 0
            rollback_target = f"{status.account or 'unknown'}/{project.name}/{args.release or 'unknown'}"
            require_approval(ApprovalScope("rollback", provider.name, rollback_target, "production"), args.approve)
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
            dry_run_failed = False
            if args.dry_run:
                data["preflight_results"] = _run_reads(runner, list(plan.preflight))
                dry_run_failed = any(item["returncode"] for item in data["preflight_results"])
                output_ready = bool(project.output_dir and (root / project.output_dir).is_dir())
                if hasattr(provider, "dry_run") and output_ready:
                    if plan.artifact_output_dir:
                        data["artifact_validation"] = validate_artifact_tree(root, plan.artifact_output_dir)
                    data["native_dry_run_results"] = _run_reads(
                        runner, list(provider.dry_run(provider_target(project, config), environment))
                    )
                    dry_run_failed = dry_run_failed or any(
                        item["returncode"] for item in data["native_dry_run_results"]
                    )
                elif hasattr(provider, "dry_run"):
                    data["native_dry_run_results"] = "Not run: build output is absent"
                else:
                    data["native_dry_run_results"] = "Provider has no supported native dry-run"
            _emit(data)
            return 1 if dry_run_failed else 0
        fresh = provider.detect(runner)
        report = execute(
            plan,
            provider,
            fresh,
            runner,
            approve=args.approve,
            expected_account=args.account,
            incident=args.incident,
            max_repair_attempts=config.max_repair_attempts,
        )
        _emit(report.to_dict())
        return 0 if report.verdict in {"HEALTHY", "DEGRADED"} else 1
    except (ValueError, PermissionError, RuntimeError, OSError, KeyError) as exc:
        _emit({"error": redact(str(exc)), "type": type(exc).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
