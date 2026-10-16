"""Classify observed failures; log text is evidence, never instructions."""

from __future__ import annotations

import re
from dataclasses import dataclass

from devops_agent.core.security import redact


@dataclass(frozen=True)
class Diagnostic:
    category: str
    hypothesis: str
    evidence: str


_RULES = (
    (
        "AUTHENTICATION_FAILURE",
        r"not logged in|authentication required|invalid token|expired token|please log in",
        "CLI authentication is missing or expired",
    ),
    (
        "PERMISSION_DENIED",
        r"access denied|permission denied|not authorized|forbidden|insufficient permission",
        "Active identity lacks permission for this operation",
    ),
    (
        "MISSING_ENVIRONMENT_VARIABLE",
        (
            r"(?:missing|undefined|not set|required).{0,50}"
            r"(?:env(?:ironment)? variable|[A-Z][A-Z0-9_]{3,})|"
            r"[A-Z][A-Z0-9_]{3,}.{0,40}(?:environment variable )?"
            r"(?:not set|missing|required)"
        ),
        "A required environment variable may be absent",
    ),
    (
        "DEPENDENCY_FAILURE",
        r"module not found|cannot find module|no module named|package .* not found|npm err!.*404",
        "A package or local dependency is missing",
    ),
    (
        "DATABASE_ERROR",
        r"(?:database|postgres|mysql|redis).{0,50}(?:connection|refused|timeout|unavailable)|migration failed",
        "Database connectivity or migration failed",
    ),
    (
        "DNS_ERROR",
        r"nxdomain|dns lookup|could not resolve host|name or service not known",
        "DNS resolution failed",
    ),
    (
        "NETWORK_ERROR",
        r"connection refused|network is unreachable|connection reset|tls handshake",
        "Network connection failed",
    ),
    (
        "CI_PIPELINE_ERROR",
        r"workflow run failed|job .* failed|github actions",
        "CI workflow or job failed",
    ),
    (
        "FRAMEWORK_CONFIGURATION_ERROR",
        r"invalid (?:next|vite|wrangler|vercel|netlify) config|output directory .* not found|no such export",
        "Framework or hosting configuration is inconsistent",
    ),
    (
        "RUNTIME_ERROR",
        r"uncaught exception|traceback \(most recent call last\)|fatal error|segmentation fault",
        "Application crashed at runtime",
    ),
    (
        "DEPLOYMENT_TIMEOUT",
        r"timed out|timeout|deadline exceeded",
        "Deployment or validation exceeded its timeout",
    ),
    (
        "POST_DEPLOYMENT_HEALTH_FAILURE",
        r"health check failed|http (?:5\d\d|4\d\d)",
        "Deployed service failed its health check",
    ),
    (
        "BUILD_FAILURE",
        r"build failed|compilation failed|syntaxerror|error ts\d{4}",
        "Build or compilation failed",
    ),
    (
        "INFRASTRUCTURE_ERROR",
        r"stack .* rollback|resource .* failed|provisioning failed",
        "Infrastructure provisioning failed",
    ),
)


def classify(output: str, returncode: int | None = None) -> Diagnostic:
    safe = redact(output)[-8192:]
    if returncode == 124:
        return Diagnostic("DEPLOYMENT_TIMEOUT", "Command exceeded its timeout", safe[-1000:])
    for category, pattern, hypothesis in _RULES:
        match = re.search(pattern, safe, re.I | re.S)
        if match:
            start = max(0, match.start() - 120)
            return Diagnostic(category, hypothesis, safe[start : match.end() + 120].strip())
    if returncode == 0:
        return Diagnostic("NONE", "No failure detected in observed output", "")
    return Diagnostic("UNKNOWN_FAILURE", "Command failed; inspect the relevant provider logs", safe[-1000:])
