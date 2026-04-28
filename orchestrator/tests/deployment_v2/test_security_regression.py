"""Security regression tests — secrets must never reach the DB or API responses."""

from __future__ import annotations

import io
import re
import zipfile

import pytest

from orchestrator.api.routes import _redact_log_secrets
from orchestrator.deployment_v2.kamal.strategy import KamalDeploymentStrategy
from orchestrator.deployment_v2.profile import DeploymentSecrets
from orchestrator.models.project import _summarize_deploy_target


CONFIG = {
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
    "secret_env_names": ["DATABASE_URL"],
    "auto_deploy_on_main": False,
    "artifacts_push_mode": "pr",
}


def test_secrets_dataclass_scrub_clears_values():
    secrets = DeploymentSecrets(
        ssh_private_key="-----KEY-----",
        registry_password="ghp_xxx",
        secret_env_values={"DATABASE_URL": "postgres://x"},
    )
    secrets.scrub()
    assert secrets.ssh_private_key == ""
    assert secrets.registry_password == ""
    assert secrets.secret_env_values == {}


def test_summarize_deploy_target_redacts_legacy_fields():
    legacy = {
        "auth_mode": "ssh_key",
        "ssh_private_key": "-----PRIVATE-----",
        "ssh_password": "p4ssw0rd",
        "host": "1.2.3.4",
        "user": "deploy",
        "deploy_path": "/srv/app",
        "env_content": "FOO=bar",
        "known_hosts": "host-key",
    }
    summary = _summarize_deploy_target(legacy, strategy="", secret_names=[])
    assert summary is not None
    serialized = repr(summary)
    assert "PRIVATE" not in serialized
    assert "p4ssw0rd" not in serialized
    assert "FOO=bar" not in serialized
    assert "host-key" not in serialized


def test_summarize_deploy_target_kamal_returns_only_safe_fields():
    summary = _summarize_deploy_target(
        CONFIG,
        strategy="kamal",
        secret_names=["KAMAL_REGISTRY_PASSWORD", "DATABASE_URL"],
        fingerprint={"branch": "chore/x"},
    )
    assert summary is not None
    assert summary["strategy"] == "kamal"
    assert "DATABASE_URL" in summary["secret_names"]
    # explicitly forbidden fields
    assert "ssh_private_key" not in summary
    assert "registry_password" not in summary


def test_workflow_yaml_references_secrets_by_name_not_value():
    strategy = KamalDeploymentStrategy()
    project = {"github_repo_owner": "owner", "github_repo_name": "repo"}
    profile = strategy.detect_requirements(project, repo_files={})
    profile = strategy.merge_config_into_profile(profile, CONFIG)
    deploy_yml = strategy.workflow_files(profile)[".github/workflows/deploy.yml"]

    # Workflow must reference secrets via ${{ secrets.NAME }}
    assert "${{ secrets.KAMAL_REGISTRY_PASSWORD }}" in deploy_yml
    assert "${{ secrets.KAMAL_SSH_PRIVATE_KEY }}" in deploy_yml
    # And must not contain literal secret-looking values.
    assert "ghp_" not in deploy_yml
    assert "PRIVATE KEY" not in deploy_yml


def test_redact_log_secrets_masks_known_names():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("0_deploy.txt", "before\nKAMAL_REGISTRY_PASSWORD=ghp_secret\nafter")
    redacted = _redact_log_secrets(buf.getvalue(), ["DATABASE_URL"])
    with zipfile.ZipFile(io.BytesIO(redacted)) as zf:
        content = zf.read("0_deploy.txt").decode("utf-8")
    assert "ghp_secret" not in content
    assert "KAMAL_REGISTRY_PASSWORD=***" in content


def test_redact_log_secrets_masks_app_secret_names():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "1_deploy.txt",
            "echo DATABASE_URL=postgres://user:pw@host/db",
        )
    redacted = _redact_log_secrets(buf.getvalue(), ["DATABASE_URL"])
    with zipfile.ZipFile(io.BytesIO(redacted)) as zf:
        content = zf.read("1_deploy.txt").decode("utf-8")
    assert "postgres://user:pw@host/db" not in content
    assert re.search(r"DATABASE_URL=\*\*\*", content)
