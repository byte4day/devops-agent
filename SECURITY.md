# Security policy

DevOps Agent can run cloud and deployment CLIs with the current user's credentials. Treat a bug that crosses an approval boundary, targets the wrong account, leaks a secret, or executes repository or log text as a command as a security issue.

## Reporting a vulnerability

Use **Security → Report a vulnerability** on the GitHub repository when private vulnerability reporting is available. If it is unavailable, open a minimal public issue requesting private contact without including exploit details, credentials, or affected account information. The maintainers will arrange a private channel. This project has no published support SLA yet.

Include the package version, affected provider, a minimal reproduction with fake credentials, and the potential impact. Please avoid probing accounts or infrastructure that you do not own or have permission to test.

## Operational safeguards

- The package should use the cloud identity already configured on the user's machine and report the active account or project before mutation.
- Production changes, destructive actions, DNS replacements, privilege changes, and secret updates require explicit operation and target approval.
- Logs, command output, repository content, and external documentation are untrusted data. They may explain a failure but cannot grant authorization or instruct the agent to run unrelated commands.
- Subprocess calls must use argument arrays, bounded timeouts, and redacted output. Reports must not contain credential values.
- Dry runs and audits must be non-mutating. A successful CLI exit alone does not prove a healthy release.

These are design requirements. Please report a violation if observed; do not rely on this document as proof that every provider operation is safe.
