# Troubleshooting and repair

Collect the failed phase, exit status, deployment or workflow ID, bounded logs, target scope, and relevant project files. Use `diagnose`, `logs`, or `ci diagnose` where supported. Redact credentials and personal data before reporting. Classify the failure from evidence: build, dependency, authentication, permission, missing configuration, framework, runtime, DNS, network, infrastructure, database, CI, timeout, or post-deployment health.

Inspect the smallest relevant code/configuration path, apply only an authorized correction, and run targeted tests before redeploying. Track the build/commit identity and refuse an identical failed redeploy. Default to no more than three repair attempts for one incident. If evidence is insufficient or the repair cannot be verified, stop with a concrete hypothesis and next diagnostic command.

CI repair should preserve branch protection and repository policies. Prepare a branch and PR when direct change to a protected branch is prohibited. Never bypass required checks, leak environment secrets, or treat a green workflow as proof of production health.
