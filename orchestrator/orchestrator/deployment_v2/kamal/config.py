"""Pydantic config schema for Kamal deployment (persisted, no secrets)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class KamalConfig(BaseModel):
    strategy: Literal["kamal"] = "kamal"
    host: str
    ssh_user: str
    ssh_port: int = 22
    domain: str
    container_port: int = 3000
    health_path: str = "/api/health"
    registry: Literal["ghcr.io"] = "ghcr.io"
    registry_username: str
    image: str | None = None
    clear_env: dict[str, str] = Field(default_factory=dict)
    secret_env_names: list[str] = Field(default_factory=list)
    auto_deploy_on_main: bool = False
    artifacts_push_mode: Literal["pr", "direct"] = "pr"

    @field_validator("host", "ssh_user", "domain", "registry_username")
    @classmethod
    def _required_non_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("ssh_port", "container_port")
    @classmethod
    def _port_range(cls, value: int) -> int:
        if value < 1 or value > 65535:
            raise ValueError("port must be 1..65535")
        return value

    @field_validator("health_path")
    @classmethod
    def _health_path_leading_slash(cls, value: str) -> str:
        cleaned = (value or "/api/health").strip()
        if not cleaned.startswith("/"):
            cleaned = "/" + cleaned
        return cleaned

    @field_validator("secret_env_names")
    @classmethod
    def _secret_names_uppercase(cls, names: list[str]) -> list[str]:
        result: list[str] = []
        for name in names:
            cleaned = (name or "").strip()
            if not cleaned:
                continue
            if not cleaned.replace("_", "").isalnum():
                raise ValueError(f"invalid secret env name: {cleaned!r}")
            result.append(cleaned.upper())
        return result
