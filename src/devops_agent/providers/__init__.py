"""Provider adapter registry."""

from . import alibaba, aws, cloudflare, github, netlify, vercel
from .base import (
    BaseProvider,
    CommandSpec,
    OperationResult,
    ProviderStatus,
    Runner,
    UnsupportedOperation,
)


def all_providers() -> list[BaseProvider]:
    return [
        cloudflare.Provider(),
        aws.Provider(),
        alibaba.Provider(),
        vercel.Provider(),
        netlify.Provider(),
        github.Provider(),
    ]


__all__ = [
    "BaseProvider",
    "CommandSpec",
    "OperationResult",
    "ProviderStatus",
    "Runner",
    "UnsupportedOperation",
    "all_providers",
]
