"""DeploymentStrategy abstract base.

A strategy owns: the persisted config schema, the secret-name schema, the
templates rendered into the child repo, the workflow_dispatch input contract,
and phase-aware preflight probes. Adding a new deploy backend = a new strategy
without touching the orchestrator core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel

from orchestrator.deployment_v2.profile import DeploymentProfile, DeploymentSecrets
from orchestrator.validation.preflight import PreflightCheck

DispatchAction = Literal["setup", "deploy", "rollback"]
PreflightPhase = Literal["generate_artifacts", "setup", "deploy", "health_verify"]


class DeploymentStrategy(ABC):
    """Pure interface — no orchestrator-side state."""

    name: str = ""
    version: str = "0"
    rollback_metadata_required: bool = False

    @abstractmethod
    def config_schema(self) -> type[BaseModel]:
        """Pydantic model that validates persisted config (no secrets)."""

    @abstractmethod
    def secret_schema(self) -> dict[str, str]:
        """Map of `env_name -> human description` for required secrets.

        Returns names only. Never values.
        """

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> list[PreflightCheck]:
        """Static validation of the config payload (no network)."""

    @abstractmethod
    def normalize_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Coerce a raw config payload into the canonical persisted shape."""

    @abstractmethod
    def detect_requirements(
        self,
        project: dict[str, Any],
        repo_files: dict[str, str] | None = None,
    ) -> DeploymentProfile:
        """Run stack detection against the child repo and produce a profile.

        `repo_files` is a `path -> content` snapshot of detector inputs (e.g.
        `package.json`, `next.config.*`). Pass `None` to let the strategy fetch
        them via the GitHub Contents API.
        """

    @abstractmethod
    def render_artifacts(self, profile: DeploymentProfile) -> dict[str, str]:
        """Render all repo-bound deploy artifacts (Dockerfile, kamal config, …).

        Returns `repo_path -> file_content`. Workflow files are NOT included
        here — see `workflow_files`.
        """

    @abstractmethod
    def workflow_files(self, profile: DeploymentProfile) -> dict[str, str]:
        """Render `.github/workflows/*.yml` files."""

    @abstractmethod
    def dispatch_inputs(
        self,
        profile: DeploymentProfile,
        action: DispatchAction,
        automatron_run_id: str,
        rollback_to: str | None = None,
    ) -> dict[str, str]:
        """Build the inputs payload for `workflow_dispatch`."""

    @abstractmethod
    async def preflight(
        self,
        profile: DeploymentProfile,
        phase: PreflightPhase,
        *,
        project: dict[str, Any] | None = None,
    ) -> list[PreflightCheck]:
        """Phase-aware preflight (DNS / SSH / registry / health probes)."""

    def secrets_payload(
        self,
        profile: DeploymentProfile,
        secrets: DeploymentSecrets,
    ) -> dict[str, str]:
        """Map declared secret names to the values supplied in `secrets`.

        Default implementation pairs `ssh_private_key`/`registry_password`
        with the conventional Kamal env names and copies `secret_env_values`
        verbatim. Subclasses override for non-standard mappings.
        """
        from orchestrator.deployment_v2.kamal.secrets import (
            KAMAL_REGISTRY_PASSWORD,
            KAMAL_SSH_PRIVATE_KEY,
        )

        payload: dict[str, str] = {
            KAMAL_REGISTRY_PASSWORD: secrets.registry_password,
            KAMAL_SSH_PRIVATE_KEY: secrets.ssh_private_key,
        }
        for name in profile.secret_env_names:
            value = secrets.secret_env_values.get(name, "")
            payload[name] = value
        return payload
