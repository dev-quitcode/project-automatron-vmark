"""Kamal-based deployment strategy."""

from __future__ import annotations

from orchestrator.deployment_v2.kamal.secrets import (
    KAMAL_REGISTRY_PASSWORD,
    KAMAL_SSH_PRIVATE_KEY,
)
from orchestrator.deployment_v2.kamal.strategy import KamalDeploymentStrategy

__all__ = [
    "KAMAL_REGISTRY_PASSWORD",
    "KAMAL_SSH_PRIVATE_KEY",
    "KamalDeploymentStrategy",
]
