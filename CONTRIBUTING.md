# Contributing to DevOps Agent

DevOps Agent handles deployment operations, so a change should be reviewable as both Python behavior and an operational decision. Small, focused pull requests are easiest to verify.

## Development setup

Use Python 3.11 or newer in a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check .
python -m build
```

The tests use local fixtures and mocks. Do not use production credentials or create billable cloud resources for routine tests.

## Before opening a pull request

1. Describe the operation, its target and environment, and the authorization boundary. A provider adapter must fail closed when account, project, or deployment identity is uncertain.
2. Link current official CLI or API documentation for every new command or option. Record the minimum verified CLI version if behavior depends on it.
3. Add a meaningful test for success, failure, unavailable CLI, missing authentication, redaction, and denied production or destructive actions where applicable.
4. Run tests, lint, and a package build. Document any check that could not run.
5. Update `README.md` and the relevant skill reference only for behavior the code actually supports.

Keep provider operations typed and pass subprocess arguments as arrays. Do not run shell strings, print secrets, hide failed checks, or turn a dry run into a mutation. Preserve the release verdict distinction between `HEALTHY`, `DEGRADED`, `FAILED`, and `UNVERIFIED`.

## Pull requests and issues

Use the issue templates for reproducible bugs and concrete feature requests. Remove account identifiers, private URLs, access tokens, environment variable values, and customer data from logs before posting. For sensitive vulnerabilities, follow [SECURITY.md](SECURITY.md).

Contributors agree that submissions are available under the [MIT License](LICENSE).
