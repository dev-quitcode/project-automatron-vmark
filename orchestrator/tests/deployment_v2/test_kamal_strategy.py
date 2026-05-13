"""Tests for KamalDeploymentStrategy."""

from __future__ import annotations

import json

import pytest

from orchestrator.deployment_v2.kamal.strategy import KamalDeploymentStrategy
from orchestrator.deployment_v2.kamal.secrets import (
    KAMAL_REGISTRY_PASSWORD,
    KAMAL_SSH_PRIVATE_KEY,
)
from orchestrator.deployment_v2.profile import DeploymentSecrets


VALID_CONFIG = {
    "strategy": "kamal",
    "host": "203.0.113.10",
    "ssh_user": "root",
    "ssh_port": 22,
    "domain": "app.example.com",
    "container_port": 3000,
    "health_path": "/api/health",
    "registry": "ghcr.io",
    "registry_username": "owner",
    "image": "ghcr.io/owner/repo",
    "clear_env": {"NODE_ENV": "production"},
    "secret_env_names": ["DATABASE_URL", "APP_SECRET"],
    "auto_deploy_on_main": False,
    "artifacts_push_mode": "pr",
}


def test_validate_config_passes_for_valid_payload():
    strategy = KamalDeploymentStrategy()
    checks = strategy.validate_config(VALID_CONFIG)
    blocking = [c for c in checks if c.status == "blocking"]
    assert blocking == []


def test_validate_config_blocks_when_required_missing():
    strategy = KamalDeploymentStrategy()
    bad = dict(VALID_CONFIG, host="")
    checks = strategy.validate_config(bad)
    assert any(c.status == "blocking" for c in checks)


def test_secret_schema_returns_only_names():
    strategy = KamalDeploymentStrategy()
    schema = strategy.secret_schema()
    assert KAMAL_REGISTRY_PASSWORD in schema
    assert KAMAL_SSH_PRIVATE_KEY in schema
    # values must be human descriptions, never sensitive content
    for description in schema.values():
        assert isinstance(description, str)
        assert "ghp_" not in description


def test_dispatch_inputs_includes_correlation_id():
    strategy = KamalDeploymentStrategy()
    project = {"github_repo_owner": "owner", "github_repo_name": "repo"}
    profile = strategy.detect_requirements(project, repo_files={})
    profile = strategy.merge_config_into_profile(profile, VALID_CONFIG)

    inputs = strategy.dispatch_inputs(
        profile, action="deploy", automatron_run_id="run-abc"
    )
    assert inputs == {"action": "deploy", "automatron_run_id": "run-abc"}


def test_dispatch_inputs_for_rollback_requires_target():
    strategy = KamalDeploymentStrategy()
    project = {"github_repo_owner": "owner", "github_repo_name": "repo"}
    profile = strategy.detect_requirements(project, repo_files={})
    profile = strategy.merge_config_into_profile(profile, VALID_CONFIG)

    with pytest.raises(ValueError):
        strategy.dispatch_inputs(profile, action="rollback", automatron_run_id="x")

    inputs = strategy.dispatch_inputs(
        profile, action="rollback", automatron_run_id="x", rollback_to="abcdef1234"
    )
    assert inputs["rollback_to"] == "abcdef1234"


def test_render_artifacts_standalone_output():
    strategy = KamalDeploymentStrategy()
    project = {"github_repo_owner": "owner", "github_repo_name": "repo"}
    profile = strategy.detect_requirements(
        project,
        repo_files={
            "package.json": json.dumps({"dependencies": {"next": "^15"}}),
            "package-lock.json": "{}",
            "next.config.mjs": "export default { output: 'standalone' };",
        },
    )
    profile = strategy.merge_config_into_profile(profile, VALID_CONFIG)

    files = strategy.render_artifacts(profile)

    assert "Dockerfile" in files
    assert "config/deploy.yml" in files
    assert "DEPLOYMENT.md" in files
    assert ".kamal/secrets.example" in files
    assert ".dockerignore" in files

    dockerfile = files["Dockerfile"]
    assert "node:22-alpine" in dockerfile
    assert "COPY . ." in dockerfile
    assert "npm prune --omit=dev" in dockerfile
    assert "node\", \"server.js" in dockerfile  # standalone path

    deploy_yml = files["config/deploy.yml"]
    assert "service:" in deploy_yml
    assert "image: owner/repo" in deploy_yml
    assert "ssl: true" in deploy_yml
    assert "app_port: 3000" in deploy_yml
    assert "- arm64" in deploy_yml
    assert "ghcr.io" in deploy_yml
    assert "DATABASE_URL" in deploy_yml


def test_render_artifacts_default_output_uses_npm_start():
    strategy = KamalDeploymentStrategy()
    project = {"github_repo_owner": "owner", "github_repo_name": "repo"}
    profile = strategy.detect_requirements(
        project,
        repo_files={
            "package.json": json.dumps({"dependencies": {"next": "^15"}}),
            "package-lock.json": "{}",
            "next.config.js": "module.exports = {};",
        },
    )
    profile = strategy.merge_config_into_profile(profile, VALID_CONFIG)

    files = strategy.render_artifacts(profile)
    dockerfile = files["Dockerfile"]
    assert "npm\", \"start" in dockerfile
    assert "RUN npm ci --omit=dev" not in dockerfile
    assert "COPY --from=builder /app/node_modules ./node_modules" in dockerfile


def test_workflow_files_render_correlation_input():
    strategy = KamalDeploymentStrategy()
    project = {"github_repo_owner": "owner", "github_repo_name": "repo"}
    profile = strategy.detect_requirements(project, repo_files={})
    profile = strategy.merge_config_into_profile(profile, VALID_CONFIG)

    files = strategy.workflow_files(profile)
    deploy_yml = files[".github/workflows/deploy.yml"]
    assert "automatron_run_id" in deploy_yml
    assert "run-name" in deploy_yml
    assert "kamal setup" in deploy_yml
    assert "kamal deploy" in deploy_yml
    assert "kamal rollback" in deploy_yml
    # auto_deploy_on_main is False -> no `push:` trigger
    assert "branches: [main]" not in deploy_yml


def test_workflow_files_with_auto_deploy_includes_push_trigger():
    strategy = KamalDeploymentStrategy()
    project = {"github_repo_owner": "owner", "github_repo_name": "repo"}
    profile = strategy.detect_requirements(project, repo_files={})
    config = dict(VALID_CONFIG, auto_deploy_on_main=True)
    profile = strategy.merge_config_into_profile(profile, config)

    deploy_yml = strategy.workflow_files(profile)[".github/workflows/deploy.yml"]
    assert "branches: [main]" in deploy_yml


def test_secrets_payload_pairs_names_with_values():
    strategy = KamalDeploymentStrategy()
    project = {"github_repo_owner": "owner", "github_repo_name": "repo"}
    profile = strategy.detect_requirements(project, repo_files={})
    profile = strategy.merge_config_into_profile(profile, VALID_CONFIG)

    secrets = DeploymentSecrets(
        ssh_private_key="-----PRIVATE KEY-----",
        registry_password="ghp_xxx",
        secret_env_values={"DATABASE_URL": "postgres://x", "APP_SECRET": "s"},
    )
    payload = strategy.secrets_payload(profile, secrets)
    assert payload[KAMAL_REGISTRY_PASSWORD] == "ghp_xxx"
    assert payload[KAMAL_SSH_PRIVATE_KEY] == "-----PRIVATE KEY-----"
    assert payload["DATABASE_URL"] == "postgres://x"
    assert payload["APP_SECRET"] == "s"
