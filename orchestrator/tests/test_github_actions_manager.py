"""Tests for GitHub Actions environment and workflow helpers."""

from __future__ import annotations

import base64

from nacl.public import PrivateKey

from orchestrator.github_actions.manager import GitHubActionsManager


def test_workflow_files_include_ci_and_deploy():
    manager = GitHubActionsManager()

    workflow_files = manager.workflow_files(environment_name="production")

    assert ".github/workflows/ci.yml" in workflow_files
    assert ".github/workflows/deploy.yml" in workflow_files
    assert "name: CI" in workflow_files[".github/workflows/ci.yml"]
    assert "environment: production" in workflow_files[".github/workflows/deploy.yml"]


def test_build_environment_secrets_requires_ssh_key_for_key_auth():
    manager = GitHubActionsManager()

    try:
        manager.build_environment_secrets(
            {
                "auth_mode": "ssh_key",
                "host": "example.com",
                "user": "deploy",
                "deploy_path": "/srv/app",
            }
        )
    except RuntimeError as exc:
        assert "ssh_private_key" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError for missing ssh_private_key")


def test_build_environment_secrets_maps_expected_values():
    manager = GitHubActionsManager()

    secrets = manager.build_environment_secrets(
        {
            "auth_mode": "ssh_key",
            "host": "example.com",
            "port": 2222,
            "user": "deploy",
            "deploy_path": "/srv/app",
            "ssh_private_key": "PRIVATE KEY",
            "known_hosts": "example.com ssh-ed25519 AAAA",
            "env_content": "APP_ENV=prod",
            "app_url": "https://example.com",
            "health_path": "/health",
        }
    )

    assert secrets["AUTOMATRON_DEPLOY_HOST"] == "example.com"
    assert secrets["AUTOMATRON_DEPLOY_PORT"] == "2222"
    assert secrets["AUTOMATRON_DEPLOY_SSH_PRIVATE_KEY"] == "PRIVATE KEY"
    assert secrets["AUTOMATRON_DEPLOY_ENV_FILE"] == "APP_ENV=prod"
    assert secrets["AUTOMATRON_APP_URL"] == "https://example.com"


def test_build_environment_secrets_supports_password_auth():
    manager = GitHubActionsManager()

    secrets = manager.build_environment_secrets(
        {
            "auth_mode": "password",
            "host": "example.com",
            "port": 22,
            "user": "root",
            "deploy_path": "/srv/app",
            "ssh_password": "super-secret",
        }
    )

    assert secrets["AUTOMATRON_DEPLOY_SSH_PASSWORD"] == "super-secret"
    assert "AUTOMATRON_DEPLOY_SSH_PRIVATE_KEY" not in secrets


def test_encrypt_secret_returns_base64_ciphertext():
    manager = GitHubActionsManager()
    private_key = PrivateKey.generate()
    public_key_b64 = base64.b64encode(bytes(private_key.public_key)).decode("ascii")

    encrypted = manager._encrypt_secret(public_key_b64, "super-secret")

    assert encrypted != "super-secret"
    assert isinstance(base64.b64decode(encrypted), bytes)


def test_match_run_by_correlation_finds_run_via_run_name(monkeypatch):
    import asyncio

    manager = GitHubActionsManager()

    responses = [
        {"workflow_runs": []},
        {
            "workflow_runs": [
                {
                    "id": 99,
                    "name": "Automatron deploy / abc-123",
                    "status": "in_progress",
                    "conclusion": None,
                    "html_url": "https://github.test/run/99",
                    "head_sha": "deadbeef",
                    "created_at": "2026-04-27T12:00:00Z",
                    "updated_at": "2026-04-27T12:00:01Z",
                }
            ]
        },
    ]

    async def fake_get(path: str, *, params=None):
        return responses.pop(0) if responses else {"workflow_runs": []}

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(manager, "_get", fake_get)
    monkeypatch.setattr("orchestrator.github_actions.manager.asyncio.sleep", fake_sleep)

    summary = asyncio.run(
        manager.match_run_by_correlation(
            "repo",
            ".github/workflows/deploy.yml",
            "abc-123",
            max_retries=3,
            delay_s=0.0,
        )
    )
    assert summary.run_id == "99"
    assert summary.status == "running"
    assert summary.run_url == "https://github.test/run/99"


def test_dispatch_workflow_posts_inputs(monkeypatch):
    import asyncio

    manager = GitHubActionsManager()
    captured: dict[str, object] = {}

    async def fake_post(path: str, *, json=None):
        captured["path"] = path
        captured["json"] = json
        return {}

    monkeypatch.setattr(manager, "_post", fake_post)

    asyncio.run(
        manager.dispatch_workflow(
            "repo",
            ".github/workflows/deploy.yml",
            ref="main",
            inputs={"action": "deploy", "automatron_run_id": "abc-123"},
        )
    )

    assert "/actions/workflows/" in captured["path"]
    assert captured["json"] == {
        "ref": "main",
        "inputs": {"action": "deploy", "automatron_run_id": "abc-123"},
    }


def test_sync_repository_prefers_feature_ci_and_main_deploy(monkeypatch):
    manager = GitHubActionsManager()

    async def fake_get_workflow_runs(repo_name: str):
        return [
            {
                "id": 11,
                "name": "CI",
                "head_branch": "feature/1-client-portal",
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://github.test/ci",
                "head_sha": "sha-ci",
                "created_at": "2026-03-10T10:00:00Z",
                "updated_at": "2026-03-10T10:01:00Z",
            },
            {
                "id": 22,
                "name": "Deploy",
                "head_branch": "main",
                "status": "in_progress",
                "conclusion": None,
                "html_url": "https://github.test/deploy",
                "head_sha": "sha-deploy",
                "created_at": "2026-03-10T10:02:00Z",
                "updated_at": "2026-03-10T10:03:00Z",
            },
        ]

    monkeypatch.setattr(manager, "_get_workflow_runs", fake_get_workflow_runs)

    result = manager.sync_repository
    payload = result("repo-name", feature_branch="feature/1-client-portal")

    # Async function returns a coroutine; execute it inline through Python's event loop helper.
    import asyncio

    data = asyncio.run(payload)

    assert data["ci"].status == "succeeded"
    assert data["ci"].run_url == "https://github.test/ci"
    assert data["deploy"].status == "running"
    assert data["deploy"].head_sha == "sha-deploy"
