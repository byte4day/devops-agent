# Changelog

All notable changes to DevOps Agent will be documented here. The project uses semantic versioning for the Python package; provider behavior is also constrained by the installed provider CLI version.

## [0.1.1] - 2026-10-17

- Return failing exit codes for unsuccessful provider reads and invalid local CI workflows.
- Reject empty or invalid GitHub Actions event triggers during local inspection.
- Lock repair incident reservations across concurrent processes and ignore untracked `.env` files when fingerprinting a candidate.
- Redact configured secret environment values even when a provider CLI prints only the value.
- Align the published project configuration schema with accepted runtime settings.

## [0.1.0] - 2026-10-17

- Initial shared Agent Skill, Python CLI, safe installer, six first-class provider adapters, deterministic HTTP verification, and GitHub Actions diagnostics.
- Added bounded repair incident tracking, static artifact upload guards, local CI setup and triage, read-only hosting/domain inspection, and a reviewed AWS CloudFormation update path.
- Cloud integrations are verified with official CLI documentation and offline fixtures; live production integration tests are pending.
