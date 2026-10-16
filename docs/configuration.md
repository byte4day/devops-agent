# Project configuration

`devops.yaml` is optional. Put it at the root of the application you want DevOps Agent to inspect. The CLI validates the file before planning a deployment and rejects unknown keys. The file is for operation settings, never credentials or authorization tokens.

```yaml
provider: vercel
project_name: example-web
default_environment: preview
allowed_targets: [preview, staging, production]
test_commands:
  - [npm, test]
build_commands:
  - [npm, run, build]
health_urls:
  - https://example.example/health
max_repair_attempts: 3
command_timeout: 300
approval_policy:
  require_production_approval: true
```

Commands are **argument arrays**, not shell lines. Write `[npm, run, build]`, not `"npm run build"`. The executor passes each array directly to the operating system without a shell. Paths and URLs should point to resources you control.

| Key | Default | Meaning |
| --- | --- | --- |
| `provider` | auto selection when unambiguous | Preferred provider name. An explicit CLI selection takes precedence. |
| `project_name` | detected name | Label for reports. |
| `default_environment` | `preview` | `preview`, `staging`, or `production`. |
| `allowed_targets` | all three environments | Local planning allowlist; it does not grant deployment approval. |
| `test_commands`, `build_commands` | detected commands | Lists of executable argument arrays. |
| `health_urls` | empty | Absolute HTTP(S) application endpoints checked for production; preview verification uses the observed preview URL. |
| `max_repair_attempts` | `3` | Bound for agent-guided repair attempts; accepted range is 0–3. |
| `command_timeout` | `300` | External command timeout in seconds; accepted range is 1–1800. |
| `approval_policy.require_production_approval` | `true` | Local policy hint. Explicit target-bound authorization is still required for production mutation. |
| `aws_bucket`, `aws_prefix`, `aws_region`, `aws_account_id` | unset | Existing S3 bucket target, isolated preview/staging prefix, region, and expected 12-digit bucket owner account. AWS upload still requires target-bound approval. |
| `oss_bucket`, `oss_prefix` | unset | Existing Alibaba OSS bucket and optional isolated key prefix. Preview/staging requires a prefix. |
| `production_branch`, `preview_branch` | unset | Cloudflare Pages branch names. Production requires an explicit branch that matches the remote project. |

The configuration cannot make a platform compatible with an unsupported runtime. For example, a static artifact upload cannot host a server-rendered application or an API process. Provider-specific configuration, such as a linked Vercel project or a Wrangler project file, remains necessary where the adapter requires it. The AWS adapter uploads static output to an existing S3 bucket; its CloudFormation support is read-only validation and event inspection. The Alibaba adapter uploads static output to an existing OSS bucket when `ossutil` is installed.

Do not commit `.env` files, access keys, session cookies, private certificate keys, or provider credential stores. Store deployment secrets using your provider's secret management mechanism and reference only environment variable **names** in source control.
