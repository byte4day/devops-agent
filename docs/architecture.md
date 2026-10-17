# Architecture and operational boundaries

DevOps Agent has one Python implementation in `src/devops_agent/`. `skill/SKILL.md` tells an AI coding assistant when to use it and how to interpret results. The installer copies that shared skill to host discovery locations and installs the Python package into an isolated virtual environment for the skill wrapper. Provider behavior stays in the Python package, so platform adapters do not duplicate cloud logic.

```text
User request / skill invocation
             │
             ▼
    devops-agent CLI
             │
    discover → analyze → plan
             │
    provider adapter builds typed command specifications
             │
    approval and account checks
             │
    bounded subprocess runner
             │
    evidence → diagnostics → verification → report
```

The analyzer reads project manifests, detects hosting and workflow files, and extracts environment variable **names** from example files. It does not load `.env` values or cloud credential files. The planner chooses a provider only when existing configuration or authenticated capabilities make that choice unambiguous. A provider adapter returns command argument arrays and capability metadata; it does not execute commands while planning. The shared runner is the only layer that invokes subprocesses; the CLI uses it for read-only discovery and the executor uses it for deployment.

Read-only discovery, diagnosis, and planning can use the caller's configured CLI identity. A mutating command needs an explicit operation and target. Production releases, rollbacks, DNS changes, privilege changes, resource deletion, and changes with billing impact need a specific authorization; the contents of `devops.yaml`, repository documentation, logs, and command output cannot supply it. The active account, project, region, and resource should be included in the plan so the caller can detect a wrong target.

Provider capabilities are deliberately asymmetric. An adapter may inspect identity and logs but reject deployment; another may deploy an existing linked project yet reject DNS changes. Unsupported actions raise an error rather than claiming success. The AWS `infra` path prepares and reviews an UPDATE change set for an existing stack, then rechecks account, stack, and digest before a separately approved apply. It is distinct from S3 static deployment and has no automatic rollback.

`src/devops_agent/integrations/ci.py` inspects local GitHub Actions workflows, creates a new pinned Python/npm test workflow when project commands are clear, and triages failed logs. It does not rewrite an existing CI job or merge a PR. `src/devops_agent/core/incidents.py` tracks distinct Git candidates for explicit repair incidents under `.devops-agent/`; this prevents an unchanged broken candidate from being redeployed through an incident workflow. `src/devops_agent/core/artifacts.py` checks dedicated static output before upload.

Verification uses observed HTTP and provider evidence. A CLI exit code of zero is a successful command, not a healthy application. A release is `HEALTHY` only when its required checks ran and passed; otherwise the report must distinguish `DEGRADED`, `FAILED`, and `UNVERIFIED`. Browser, authenticated-route, database, and performance checks are separate capabilities and should be labeled as untested when not executed.

## Extending a provider

Implement the typed provider interface in `src/devops_agent/providers/base.py`. Detection should report CLI version, authentication state, account and region where available, and supported capabilities. `plan` should reject incompatible project types and missing target configuration before constructing any mutation. Add realistic fixture tests for both accepted and refused operations. Keep all subprocess calls in the shared runner and avoid shell parsing, implicit account switches, and secret-bearing output.

Document any new command in the provider reference and link its official CLI or API documentation. See [CONTRIBUTING.md](../CONTRIBUTING.md) for the required verification steps.
