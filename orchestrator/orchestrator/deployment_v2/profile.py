"""Deployment profile, secrets, and artifact fingerprint dataclasses.

`DeploymentProfile` is safe to persist (no secret values).
`DeploymentSecrets` is write-only — never serialized to DB; scrubbed after upsert.
`ArtifactFingerprint` records what was generated and pushed to the child repo.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any


@dataclass
class DeploymentProfile:
    """Persisted deployment configuration (no secrets).

    Fields here are safe to serialize into `deployment_profile_json`. Secret
    values live in `DeploymentSecrets` and are pushed straight to GitHub
    Environment Secrets.
    """

    strategy: str
    framework: str
    package_manager: str
    router_style: str
    src_layout: bool
    next_output: str
    health_route_files: list[str] = field(default_factory=list)
    host: str = ""
    ssh_user: str = ""
    ssh_port: int = 22
    domain: str = ""
    container_port: int = 3000
    health_path: str = "/api/health"
    registry: str = "ghcr.io"
    registry_username: str = ""
    image: str = ""
    clear_env: dict[str, str] = field(default_factory=dict)
    secret_env_names: list[str] = field(default_factory=list)
    auto_deploy_on_main: bool = False
    artifacts_push_mode: str = "pr"
    kamal_version: str = "2.4.0"
    environment_name: str = "production"
    service_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeploymentProfile":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def hash(self) -> str:
        """Stable hash of the profile content (used in ArtifactFingerprint)."""
        payload = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class DeploymentSecrets:
    """Write-only secrets accepted from API and immediately upserted.

    Call `scrub()` after upsert to overwrite values in memory. The dataclass
    is intentionally not JSON-serializable through any model layer.
    """

    ssh_private_key: str = ""
    registry_password: str = ""
    secret_env_values: dict[str, str] = field(default_factory=dict)

    def scrub(self) -> None:
        self.ssh_private_key = ""
        self.registry_password = ""
        for key in list(self.secret_env_values.keys()):
            self.secret_env_values[key] = ""
        self.secret_env_values.clear()


@dataclass
class ArtifactFingerprint:
    """Records what was rendered and pushed for a given deploy artifacts run."""

    commit_sha: str
    branch: str
    pr_url: str | None
    template_version: str
    strategy_version: str
    profile_hash: str
    rendered_files: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactFingerprint":
        return cls(
            commit_sha=data.get("commit_sha", ""),
            branch=data.get("branch", ""),
            pr_url=data.get("pr_url"),
            template_version=data.get("template_version", ""),
            strategy_version=data.get("strategy_version", ""),
            profile_hash=data.get("profile_hash", ""),
            rendered_files=list(data.get("rendered_files", [])),
        )


def clone_profile_with(profile: DeploymentProfile, **overrides: Any) -> DeploymentProfile:
    return replace(profile, **overrides)
