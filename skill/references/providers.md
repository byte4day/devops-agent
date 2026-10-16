# Provider capability boundaries

Run `doctor` for installed CLI versions, authentication, account, and region. Run `plan` to see whether the chosen provider supports the detected execution model. A provider listed by the package is not necessarily authenticated or capable of every deployment mode.

Cloudflare, AWS, Alibaba Cloud, Vercel, Netlify, and GitHub have distinct resource models. Use the package's reported capabilities and current provider documentation before changing bindings, domains, environment variables, IAM, or infrastructure. The current `domain inspect HOST` performs DNS resolution and HTTPS/HTTP checks; it does not edit or enumerate provider DNS records. GitHub Actions `ci status` and `ci diagnose --run-id ID` are read-only until a repository fix is separately authorized; a successful workflow run does not prove application health. Infrastructure managed by Terraform or OpenTofu should be changed through its plan/apply lifecycle, with appropriate approval.

When auth is missing, report the provider's official interactive login path and let the user perform it locally. Never ask for credentials in chat. Do not install missing CLIs automatically.
