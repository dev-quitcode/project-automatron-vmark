"""Deployment strategies (v2) — deterministic, template-driven deploy pipeline.

Replaces the legacy [orchestrator/orchestrator/deployment/manager.py](orchestrator/orchestrator/deployment/manager.py)
SSH path. Lives under `deployment_v2/` to coexist with the legacy package
during the transition to Kamal.
"""

from __future__ import annotations

from orchestrator.deployment_v2.profile import (
    ArtifactFingerprint,
    DeploymentProfile,
    DeploymentSecrets,
)
from orchestrator.deployment_v2.registry import StrategyRegistry, get_strategy
from orchestrator.deployment_v2.strategy import DeploymentStrategy
from orchestrator.deployment_v2.templates import TEMPLATES_VERSION, TemplateRenderer

__all__ = [
    "ArtifactFingerprint",
    "DeploymentProfile",
    "DeploymentSecrets",
    "DeploymentStrategy",
    "StrategyRegistry",
    "TEMPLATES_VERSION",
    "TemplateRenderer",
    "get_strategy",
]
