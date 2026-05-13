from __future__ import annotations

import pytest
from fastapi import HTTPException

from orchestrator.api import routes


@pytest.mark.asyncio
async def test_api_setup_returns_409_when_deploy_audit_issue_open(monkeypatch):
    async def fake_get_required_project(project_id: str):
        return {
            "id": project_id,
            "deploy_audit_issue_number": 12,
            "deploy_audit_issue_url": "https://github.com/o/r/issues/12",
            "deploy_audit_gate_status": "pending",
        }

    async def fake_orch_deploy(project_id: str, *, action: str = "deploy", rollback_to=None):
        raise RuntimeError("deploy_audit_issue_open")

    monkeypatch.setattr(routes, "_get_required_project", fake_get_required_project)
    monkeypatch.setattr(routes, "orch_deploy", fake_orch_deploy)

    with pytest.raises(HTTPException) as exc:
        await routes.api_setup("p1")

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "deploy_audit_issue_open"
    assert exc.value.detail["issue_number"] == 12


@pytest.mark.asyncio
async def test_api_deploy_returns_409_when_deploy_audit_issue_missing(monkeypatch):
    async def fake_get_required_project(project_id: str):
        return {
            "id": project_id,
            "deploy_audit_issue_number": None,
            "deploy_audit_issue_url": None,
            "deploy_audit_gate_status": "missing",
        }

    async def fake_orch_deploy(project_id: str, *, action: str = "deploy", rollback_to=None):
        raise RuntimeError("deploy_audit_issue_missing")

    monkeypatch.setattr(routes, "_get_required_project", fake_get_required_project)
    monkeypatch.setattr(routes, "orch_deploy", fake_orch_deploy)

    with pytest.raises(HTTPException) as exc:
        await routes.api_deploy("p1")

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "deploy_audit_issue_missing"
    assert exc.value.detail["state"] == "missing"


@pytest.mark.asyncio
async def test_api_generate_artifacts_includes_deploy_audit_issue_payload(monkeypatch):
    async def fake_get_required_project(project_id: str):
        return {"id": project_id}

    async def fake_generate(project_id: str):
        return {
            "fingerprint": {"commit_sha": "abc", "branch": "b"},
            "deploy_audit_issue": {
                "number": 33,
                "url": "https://github.com/o/r/issues/33",
                "state": "open",
                "gate_status": "pending",
            },
        }

    monkeypatch.setattr(routes, "_get_required_project", fake_get_required_project)
    monkeypatch.setattr(routes, "orch_generate_deploy_artifacts", fake_generate)

    result = await routes.api_generate_deploy_artifacts("p1")
    assert result["status"] == "generated"
    assert result["fingerprint"]["commit_sha"] == "abc"
    assert result["deploy_audit_issue"]["number"] == 33
