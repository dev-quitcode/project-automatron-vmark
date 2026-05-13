from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.orchestrator import (
    _build_deploy_audit_issue_body,
    _read_deploy_audit_issue_state,
    _upsert_deploy_audit_issue,
)
from orchestrator.validation.preflight import PreflightService


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        strategy="kamal",
        framework="nextjs",
        package_manager="npm",
        router_style="app",
        next_output="standalone",
        host="203.0.113.10",
        domain="app.example.com",
        ssh_user="root",
        ssh_port=22,
        container_port=3000,
        health_path="/api/health",
        registry="ghcr.io",
        registry_username="owner",
        image="ghcr.io/owner/repo",
        artifacts_push_mode="pr",
        auto_deploy_on_main=False,
    )


def test_deploy_audit_issue_body_contains_secret_names_not_values():
    project = {
        "name": "demo",
        "github_repo_owner": "owner",
        "github_repo_name": "repo",
        "deployment_secret_names": ["DATABASE_URL", "KAMAL_SSH_PRIVATE_KEY"],
        # simulate legacy/mistaken values in memory; body must still avoid them
        "secret_env_values": {"DATABASE_URL": "postgres://super-secret"},
    }
    body = _build_deploy_audit_issue_body(
        project,
        _profile(),
        {
            "commit_sha": "abc",
            "branch": "chore/automatron-deploy-artifacts",
            "pr_url": "https://github.com/owner/repo/pull/1",
            "template_version": "1",
            "strategy_version": "1",
            "profile_hash": "hash",
            "rendered_files": ["Dockerfile"],
        },
    )
    assert "DATABASE_URL" in body
    assert "postgres://super-secret" not in body


@pytest.mark.asyncio
async def test_upsert_deploy_audit_issue_creates_when_missing(monkeypatch):
    class FakeGH:
        def __init__(self):
            self.created = False
            self.updated = False
            self.assigned = False

        async def get_issue(self, owner, repo, number):
            raise AssertionError("get_issue should not be called when issue_number is missing")

        async def ensure_label(self, owner, repo, name, color="ededed"):
            return None

        async def create_issue(self, owner, repo, title, body, labels=None, **kwargs):
            self.created = True
            assert "DATABASE_URL" in body
            return {"number": 77, "html_url": "https://github.com/o/r/issues/77", "state": "open"}

        async def update_issue(self, owner, repo, issue_number, **kwargs):
            self.updated = True
            return {"number": issue_number, "html_url": "https://github.com/o/r/issues/77", "state": "open"}

        async def trigger_copilot_agent(self, owner, repo, issue_number):
            self.assigned = True

    fake_gh = FakeGH()
    updates: list[dict] = []

    async def fake_update_project_deployment(project_id: str, **kwargs):
        updates.append(kwargs)

    monkeypatch.setattr("orchestrator.orchestrator.GitHubClient", lambda: fake_gh)
    monkeypatch.setattr(
        "orchestrator.models.project.update_project_deployment",
        fake_update_project_deployment,
    )

    result = await _upsert_deploy_audit_issue(
        project_id="p1",
        project={
            "name": "demo",
            "github_repo_owner": "owner",
            "github_repo_name": "repo",
            "deployment_secret_names": ["DATABASE_URL"],
        },
        profile=_profile(),
        fingerprint={"commit_sha": "abc"},
    )

    assert fake_gh.created is True
    assert fake_gh.updated is False
    assert fake_gh.assigned is True
    assert result["number"] == 77
    assert result["gate_status"] == "pending"
    assert updates[-1]["deploy_audit_issue_number"] == 77


@pytest.mark.asyncio
async def test_upsert_deploy_audit_issue_reuses_open_issue(monkeypatch):
    class FakeGH:
        def __init__(self):
            self.created = False
            self.updated = False

        async def get_issue(self, owner, repo, number):
            return {"number": number, "html_url": "https://github.com/o/r/issues/88", "state": "open"}

        async def ensure_label(self, owner, repo, name, color="ededed"):
            return None

        async def create_issue(self, owner, repo, title, body, labels=None, **kwargs):
            self.created = True
            return {"number": 89, "html_url": "https://github.com/o/r/issues/89", "state": "open"}

        async def update_issue(self, owner, repo, issue_number, **kwargs):
            self.updated = True
            return {"number": issue_number, "html_url": "https://github.com/o/r/issues/88", "state": "open"}

        async def trigger_copilot_agent(self, owner, repo, issue_number):
            return None

    fake_gh = FakeGH()

    async def fake_update_project_deployment(project_id: str, **kwargs):
        return None

    monkeypatch.setattr("orchestrator.orchestrator.GitHubClient", lambda: fake_gh)
    monkeypatch.setattr(
        "orchestrator.models.project.update_project_deployment",
        fake_update_project_deployment,
    )

    result = await _upsert_deploy_audit_issue(
        project_id="p1",
        project={
            "name": "demo",
            "github_repo_owner": "owner",
            "github_repo_name": "repo",
            "deploy_audit_issue_number": 88,
            "deploy_audit_issue_url": "https://github.com/o/r/issues/88",
            "deployment_secret_names": ["DATABASE_URL"],
        },
        profile=_profile(),
        fingerprint={"commit_sha": "abc"},
    )

    assert fake_gh.updated is True
    assert fake_gh.created is False
    assert result["number"] == 88


@pytest.mark.asyncio
async def test_upsert_deploy_audit_issue_creates_new_when_previous_closed(monkeypatch):
    class FakeGH:
        async def get_issue(self, owner, repo, number):
            return {"number": number, "html_url": "https://github.com/o/r/issues/90", "state": "closed"}

        async def ensure_label(self, owner, repo, name, color="ededed"):
            return None

        async def create_issue(self, owner, repo, title, body, labels=None, **kwargs):
            return {"number": 91, "html_url": "https://github.com/o/r/issues/91", "state": "open"}

        async def update_issue(self, owner, repo, issue_number, **kwargs):
            raise AssertionError("update_issue should not run for closed previous issue")

        async def trigger_copilot_agent(self, owner, repo, issue_number):
            return None

    async def fake_update_project_deployment(project_id: str, **kwargs):
        return None

    monkeypatch.setattr("orchestrator.orchestrator.GitHubClient", lambda: FakeGH())
    monkeypatch.setattr(
        "orchestrator.models.project.update_project_deployment",
        fake_update_project_deployment,
    )

    result = await _upsert_deploy_audit_issue(
        project_id="p1",
        project={
            "name": "demo",
            "github_repo_owner": "owner",
            "github_repo_name": "repo",
            "deploy_audit_issue_number": 90,
            "deploy_audit_issue_url": "https://github.com/o/r/issues/90",
            "deployment_secret_names": ["DATABASE_URL"],
        },
        profile=_profile(),
        fingerprint={"commit_sha": "abc"},
    )
    assert result["number"] == 91


@pytest.mark.asyncio
async def test_read_deploy_audit_issue_state_reports_missing_open_and_closed(monkeypatch):
    missing = await _read_deploy_audit_issue_state(
        {
            "github_repo_owner": "owner",
            "github_repo_name": "repo",
        }
    )
    assert missing["ok"] is False
    assert missing["code"] == "deploy_audit_issue_missing"

    class OpenGH:
        async def get_issue(self, owner, repo, number):
            return {"number": number, "state": "open", "html_url": "https://github.com/o/r/issues/7"}

    monkeypatch.setattr("orchestrator.orchestrator.GitHubClient", lambda: OpenGH())
    opened = await _read_deploy_audit_issue_state(
        {
            "github_repo_owner": "owner",
            "github_repo_name": "repo",
            "deploy_audit_issue_number": 7,
            "deploy_audit_issue_url": "https://github.com/o/r/issues/7",
        }
    )
    assert opened["ok"] is False
    assert opened["code"] == "deploy_audit_issue_open"

    class ClosedGH:
        async def get_issue(self, owner, repo, number):
            return {"number": number, "state": "closed", "html_url": "https://github.com/o/r/issues/7"}

    monkeypatch.setattr("orchestrator.orchestrator.GitHubClient", lambda: ClosedGH())
    closed = await _read_deploy_audit_issue_state(
        {
            "github_repo_owner": "owner",
            "github_repo_name": "repo",
            "deploy_audit_issue_number": 7,
            "deploy_audit_issue_url": "https://github.com/o/r/issues/7",
        }
    )
    assert closed["ok"] is True
    assert closed["gate_status"] == "ready"


@pytest.mark.asyncio
async def test_preflight_deploy_audit_gate_blocking_and_ok(monkeypatch):
    service = PreflightService()

    class OpenGH:
        async def get_issue(self, owner, repo, number):
            return {"number": number, "state": "open", "html_url": "https://github.com/o/r/issues/42"}

    monkeypatch.setattr("orchestrator.validation.preflight.GitHubClient", lambda: OpenGH())
    blocked = await service._deploy_audit_gate_check(
        {
            "github_repo_owner": "owner",
            "github_repo_name": "repo",
            "deploy_audit_issue_number": 42,
            "deploy_audit_issue_url": "https://github.com/o/r/issues/42",
        }
    )
    assert blocked.status == "blocking"
    assert blocked.code == "deploy_audit_issue_open"

    class ClosedGH:
        async def get_issue(self, owner, repo, number):
            return {"number": number, "state": "closed", "html_url": "https://github.com/o/r/issues/42"}

    monkeypatch.setattr("orchestrator.validation.preflight.GitHubClient", lambda: ClosedGH())
    ok = await service._deploy_audit_gate_check(
        {
            "github_repo_owner": "owner",
            "github_repo_name": "repo",
            "deploy_audit_issue_number": 42,
            "deploy_audit_issue_url": "https://github.com/o/r/issues/42",
        }
    )
    assert ok.status == "ok"
    assert ok.code == "deploy_audit_gate"
