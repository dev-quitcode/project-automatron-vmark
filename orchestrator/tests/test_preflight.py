"""Tests for structured preflight validation."""

from __future__ import annotations

import pytest
from docker.errors import ImageNotFound

from orchestrator.config import settings
from orchestrator.validation.preflight import PreflightService


class _FakeImagesMissing:
    def get(self, image: str) -> None:
        raise ImageNotFound("missing")


class _FakeImagesPresent:
    def get(self, image: str) -> dict[str, str]:
        return {"image": image}


class _FakeDockerClient:
    def __init__(self, images: object) -> None:
        self.images = images

    def ping(self) -> bool:
        return True


class _FakeContainerManager:
    def __init__(self, client: object) -> None:
        self.client = client


class _FakeActionsManager:
    async def ensure_environment(self, repo_name: str, *, environment_name: str | None = None) -> None:
        return None

    async def get_environment_public_key(
        self,
        repo_name: str,
        *,
        environment_name: str | None = None,
    ) -> dict[str, str]:
        raise RuntimeError("forbidden")


class _FakeRepoManager:
    def __init__(self) -> None:
        self.actions_manager = _FakeActionsManager()

    async def get_remote_repository(self, repo_name: str) -> dict[str, str] | None:
        return {"html_url": f"https://example.com/{repo_name}"}


@pytest.mark.asyncio
async def test_start_preflight_blocks_on_missing_golden_image(monkeypatch):
    monkeypatch.setattr(settings, "github_token", "gh-token")
    monkeypatch.setattr(settings, "github_owner", "dev-quitcode")
    monkeypatch.setattr(settings, "openai_api_key", "openai-key")
    async def fake_catalog(provider: str, *, force_refresh: bool = False) -> dict[str, object]:
        return {
            "provider": provider,
            "configured": True,
            "models": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-mini"}],
        }
    monkeypatch.setattr("orchestrator.validation.preflight.get_provider_model_catalog", fake_catalog)

    service = PreflightService(
        container_manager=_FakeContainerManager(_FakeDockerClient(_FakeImagesMissing())),
    )

    result = await service.run(
        "start",
        project={
            "llm_config": {
                "architect": {"provider": "openai", "model": "gpt-4.1"},
                "builder": {"provider": "openai", "model": "gpt-4.1-mini"},
                "reviewer": {"provider": "openai", "model": "gpt-4.1-mini"},
            }
        },
    )

    assert result.blocking is True
    codes = {check.code for check in result.checks}
    assert "golden_image_missing" in codes


@pytest.mark.asyncio
async def test_start_preflight_blocks_on_missing_provider_key(monkeypatch):
    monkeypatch.setattr(settings, "github_token", "gh-token")
    monkeypatch.setattr(settings, "github_owner", "dev-quitcode")
    monkeypatch.setattr(settings, "openai_api_key", "openai-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    async def fake_catalog(provider: str, *, force_refresh: bool = False) -> dict[str, object]:
        return {
            "provider": provider,
            "configured": True,
            "models": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-mini"}],
        }
    monkeypatch.setattr("orchestrator.validation.preflight.get_provider_model_catalog", fake_catalog)

    service = PreflightService(
        container_manager=_FakeContainerManager(_FakeDockerClient(_FakeImagesPresent())),
    )

    result = await service.run(
        "start",
        project={
            "llm_config": {
                "architect": {"provider": "openai", "model": "gpt-4.1"},
                "builder": {"provider": "anthropic", "model": "anthropic/claude-sonnet-4"},
                "reviewer": {"provider": "openai", "model": "gpt-4.1-mini"},
            }
        },
    )

    assert result.blocking is True
    codes = {check.code for check in result.checks}
    assert "llm_provider_builder_missing_key" in codes


def test_deploy_target_shape_validation_normalizes_health_path():
    service = PreflightService(container_manager=_FakeContainerManager(None))

    checks = service.validate_deploy_target_shape(
        {
            "auth_mode": "password",
            "host": "91.98.68.42",
            "user": "root",
            "deploy_path": "/opt/app",
            "ssh_password": "secret",
            "health_path": "",
        }
    )

    health_check = next(check for check in checks if check.code == "deploy_target_health_path")
    assert health_check.details["health_path"] == "/api/health"


@pytest.mark.asyncio
async def test_start_preflight_blocks_on_unavailable_model(monkeypatch):
    monkeypatch.setattr(settings, "github_token", "gh-token")
    monkeypatch.setattr(settings, "github_owner", "dev-quitcode")
    monkeypatch.setattr(settings, "anthropic_api_key", "anthropic-key")

    async def fake_catalog(provider: str, *, force_refresh: bool = False) -> dict[str, object]:
        return {
            "provider": provider,
            "configured": True,
            "models": [{"id": "anthropic/claude-opus-4-20250514"}],
        }

    monkeypatch.setattr("orchestrator.validation.preflight.get_provider_model_catalog", fake_catalog)

    service = PreflightService(
        container_manager=_FakeContainerManager(_FakeDockerClient(_FakeImagesPresent())),
    )

    result = await service.run(
        "start",
        project={
            "llm_config": {
                "architect": {"provider": "anthropic", "model": "anthropic/claude-opus-4-20250918"},
                "builder": {"provider": "anthropic", "model": "anthropic/claude-opus-4-20250514"},
                "reviewer": {"provider": "anthropic", "model": "anthropic/claude-opus-4-20250514"},
            }
        },
    )

    assert result.blocking is True
    codes = {check.code for check in result.checks}
    assert "llm_model_architect_unavailable" in codes


@pytest.mark.asyncio
async def test_deploy_preflight_blocks_when_environment_public_key_access_fails(monkeypatch):
    monkeypatch.setattr(settings, "github_token", "gh-token")
    monkeypatch.setattr(settings, "github_owner", "dev-quitcode")

    service = PreflightService(
        container_manager=_FakeContainerManager(_FakeDockerClient(_FakeImagesPresent())),
        repository_manager=_FakeRepoManager(),
    )

    result = await service.run(
        "deploy",
        project={
            "repo_name": "example-repo",
            "deploy_target": {
                "auth_mode": "password",
                "host": "91.98.68.42",
                "user": "root",
                "deploy_path": "/opt/app",
                "ssh_password": "secret",
            },
            "github_environment_name": "production",
        },
    )

    assert result.blocking is True
    codes = {check.code for check in result.checks}
    assert "deploy_environment_access_failed" in codes
