"""Tests for runner resume semantics and canonical state patches."""

from __future__ import annotations

import pytest

from orchestrator.graph import runner
from orchestrator.validation.preflight import PreflightCheck, PreflightResult


class _DummyTask:
    def cancel(self) -> None:
        return None


def _fake_create_task(coro):
    coro.close()
    return _DummyTask()


@pytest.fixture(autouse=True)
def _clear_active_runs():
    runner._active_runs.clear()
    yield
    runner._active_runs.clear()


@pytest.mark.asyncio
async def test_start_project_starts_clean_intake(monkeypatch):
    stage_calls: list[str] = []
    status_calls: list[str] = []

    async def fake_get_project(project_id: str) -> dict[str, object]:
        return {
            "id": project_id,
            "name": "Test Project",
            "status": "pending",
            "project_stage": "intake",
            "intake_text": "Build something",
            "llm_config": {},
        }

    async def fake_preflight(phase: str, *, project: dict[str, object]) -> PreflightResult:
        return PreflightResult(
            phase="start",
            ok=True,
            blocking=False,
            checks=[PreflightCheck(code="ok", status="ok", message="ok")],
        )

    async def fake_update_stage(project_id: str, stage: str) -> None:
        stage_calls.append(stage)

    async def fake_update_status(project_id: str, status: str) -> None:
        status_calls.append(status)

    async def fake_checkpoint(project_id: str):
        return None

    monkeypatch.setattr(runner, "get_project", fake_get_project)
    monkeypatch.setattr(runner.preflight_service, "run", fake_preflight)
    monkeypatch.setattr(runner, "get_latest_checkpoint_summary", fake_checkpoint)
    monkeypatch.setattr(runner, "update_project_stage", fake_update_stage)
    monkeypatch.setattr(runner, "update_project_status", fake_update_status)
    monkeypatch.setattr(runner.asyncio, "create_task", _fake_create_task)

    result = await runner.start_project("project-1")

    assert result["status"] == "started"
    assert stage_calls == ["planning"]
    assert status_calls == ["planning"]


@pytest.mark.asyncio
async def test_start_project_resumes_from_checkpoint_without_resetting_plan(monkeypatch):
    stage_calls: list[str] = []
    status_calls: list[str] = []

    async def fake_get_project(project_id: str) -> dict[str, object]:
        return {
            "id": project_id,
            "name": "Test Project",
            "status": "paused",
            "project_stage": "error",
            "plan_md": "# PLAN\n- [ ] Task\n",
            "stack_config": {"framework": "nextjs"},
            "llm_config": {},
            "deploy_target": {},
            "preview_url": "",
            "preview_status": "pending",
            "preview_metadata": {},
            "plan_approved": True,
            "preview_approved": False,
            "repo_name": "example-repo",
            "repo_url": "https://example.com/repo",
            "repo_clone_url": "https://example.com/repo.git",
            "default_branch": "main",
            "develop_branch": "develop",
            "feature_branch": "feature/1-test-project",
            "repo_ready": True,
            "container_id": "container-1",
            "port": 7001,
        }

    async def fake_preflight(phase: str, *, project: dict[str, object]) -> PreflightResult:
        return PreflightResult(
            phase="start",
            ok=True,
            blocking=False,
            checks=[PreflightCheck(code="ok", status="ok", message="ok")],
        )

    async def fake_update_stage(project_id: str, stage: str) -> None:
        stage_calls.append(stage)

    async def fake_update_status(project_id: str, status: str) -> None:
        status_calls.append(status)

    async def fake_checkpoint(project_id: str):
        return {"project_stage": "building", "status": "building"}

    monkeypatch.setattr(runner, "get_project", fake_get_project)
    monkeypatch.setattr(runner.preflight_service, "run", fake_preflight)
    monkeypatch.setattr(runner, "get_latest_checkpoint_summary", fake_checkpoint)
    monkeypatch.setattr(runner, "update_project_stage", fake_update_stage)
    monkeypatch.setattr(runner, "update_project_status", fake_update_status)
    monkeypatch.setattr(runner.asyncio, "create_task", _fake_create_task)

    result = await runner.start_project("project-1")

    assert result["status"] == "resumed"
    assert stage_calls == ["building"]
    assert status_calls == ["building"]


def test_build_resume_state_patch_is_canonical():
    project = {
        "name": "Test Project",
        "plan_md": "# PLAN",
        "stack_config": {"framework": "nextjs"},
        "llm_config": {},
        "execution_contract": {"task_graph": [{"task_id": "task-001"}]},
        "contract_version": 3,
        "decision_log": [{"id": "decision-001"}],
        "plan_delta_history": [{"type": "plan_delta"}],
        "deploy_target": {"host": "example.com"},
        "preview_url": "http://localhost:7001",
        "preview_status": "healthy",
        "preview_metadata": {"pid": "123"},
        "plan_approved": True,
        "preview_approved": False,
        "active_task_id": "task-001",
        "task_attempt_count": 2,
        "task_validation_result": {"blocking": True},
        "last_escalation": {"task_id": "task-001"},
        "builder_report": {"status": "failed"},
        "repo_name": "repo",
        "repo_url": "https://example.com/repo",
        "repo_clone_url": "https://example.com/repo.git",
        "default_branch": "main",
        "develop_branch": "develop",
        "feature_branch": "feature/1-test-project",
        "repo_ready": True,
        "container_id": "container-1",
        "port": 7001,
    }

    patch = runner._build_resume_state_patch(project)

    assert patch["plan_md"] == "# PLAN"
    assert patch["execution_contract"] == {"task_graph": [{"task_id": "task-001"}]}
    assert patch["contract_version"] == 3
    assert patch["preview_metadata"] == {"pid": "123"}
    assert patch["container_port"] == 7001
    assert patch["feature_branch"] == "feature/1-test-project"
    assert patch["active_task_id"] == "task-001"
    assert patch["task_attempt_count"] == 2


@pytest.mark.asyncio
async def test_get_task_status_uses_execution_contract(monkeypatch):
    async def fake_get_project(project_id: str) -> dict[str, object]:
        return {
            "id": project_id,
            "execution_contract": {
                "task_graph": [
                    {"task_id": "task-001", "completed": True, "status": "completed"},
                    {"task_id": "task-002", "completed": False, "status": "retrying"},
                ]
            },
            "active_task_id": "task-002",
            "task_attempt_count": 2,
            "task_validation_result": {"blocking": True},
            "builder_report": {"status": "failed"},
            "last_escalation": {"task_id": "task-002"},
        }

    monkeypatch.setattr(runner, "get_project", fake_get_project)

    result = await runner.get_task_status("project-1")

    assert result["active_task_id"] == "task-002"
    assert result["active_task"]["task_id"] == "task-002"
    assert result["completed_tasks"] == 1
    assert result["total_tasks"] == 2
    assert result["task_attempt_count"] == 2


@pytest.mark.asyncio
async def test_restart_preview_updates_stage_and_status(monkeypatch):
    stage_calls: list[str] = []
    status_calls: list[str] = []
    preview_updates: list[tuple[str, str, str, dict[str, object]]] = []
    emitted: list[dict[str, object]] = []

    async def fake_get_project(project_id: str) -> dict[str, object]:
        return {
            "id": project_id,
            "container_id": "container-1",
            "port": 7001,
            "stack_config": {"port": 3000},
            "preview_url": "",
            "status": "building",
            "project_stage": "building",
        }

    class _RuntimeSpec:
        readiness_path = "/api/health"

    class _ValidationResult:
        blocking_issues: list[object] = []
        runtime_spec = _RuntimeSpec()

    async def fake_validate(*args, **kwargs):
        return _ValidationResult()

    async def fake_start_preview(*args, **kwargs):
        return {"pid": "123"}

    async def fake_wait_for_preview(*args, **kwargs):
        return None

    async def fake_update_preview(project_id: str, url: str, status: str, metadata: dict[str, object]) -> None:
        preview_updates.append((project_id, url, status, metadata))

    async def fake_update_stage(project_id: str, stage: str) -> None:
        stage_calls.append(stage)

    async def fake_update_status(project_id: str, status: str) -> None:
        status_calls.append(status)

    async def fake_emit_status_update(project_id: str, **payload) -> None:
        emitted.append({"project_id": project_id, **payload})

    monkeypatch.setattr(runner, "get_project", fake_get_project)
    monkeypatch.setattr(runner, "validate_workspace_contract_async", fake_validate)
    monkeypatch.setattr(runner.container_manager, "start_preview_process", fake_start_preview)
    monkeypatch.setattr(runner.container_manager, "wait_for_preview", fake_wait_for_preview)
    monkeypatch.setattr(runner, "update_project_preview", fake_update_preview)
    monkeypatch.setattr(runner, "update_project_stage", fake_update_stage)
    monkeypatch.setattr(runner, "update_project_status", fake_update_status)
    monkeypatch.setattr(runner, "emit_status_update", fake_emit_status_update)

    result = await runner.restart_preview("project-1")

    assert result["status"] == "restarted"
    assert preview_updates
    assert stage_calls == ["awaiting_preview_approval"]
    assert status_calls == ["preview"]
    assert emitted == [
        {
            "project_id": "project-1",
            "status": "preview",
            "stage": "awaiting_preview_approval",
            "progress": {},
            "preview_url": "http://localhost:7001",
        }
    ]
