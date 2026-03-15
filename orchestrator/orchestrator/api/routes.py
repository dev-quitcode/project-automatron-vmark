"""REST API routes for project lifecycle management."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from orchestrator.graph.runner import (
    _aget_graph,
    _make_thread_config,
    deploy_project as runner_deploy,
    get_checkpoints,
    get_runtime_project as runner_get_runtime_project,
    get_task_status as runner_get_task_status,
    is_running,
    restart_preview as runner_restart_preview,
    resume_project as runner_resume,
    run_preflight as runner_preflight,
    sync_cicd_status as runner_sync_cicd,
    start_project as runner_start,
    stop_project as runner_stop,
)
from orchestrator.llm.catalog import get_all_provider_model_catalogs, get_provider_model_catalog
from orchestrator.llm.configuration import default_llm_config, normalize_llm_config
from orchestrator.repository.manager import RepositoryManager
from orchestrator.validation.preflight import PreflightService
from orchestrator.models.project import (
    create_project,
    get_all_projects,
    get_chat_messages,
    get_deploy_runs,
    get_project,
    get_task_logs,
    get_trace_events,
    sync_project_from_state,
    update_project_deploy_target,
    update_project_llm_config,
    update_project_plan,
    update_project_stage,
    update_project_status,
)
from orchestrator.models.session import get_sessions

router = APIRouter()
repository_manager = RepositoryManager()
preflight_service = PreflightService(repository_manager=repository_manager)


class RoleLlmConfig(BaseModel):
    provider: str
    model: str


class ProjectLlmConfigRequest(BaseModel):
    architect: RoleLlmConfig
    builder: RoleLlmConfig
    reviewer: RoleLlmConfig


class CreateProjectRequest(BaseModel):
    name: str
    intake_text: str | None = None
    description: str | None = None
    source: str = "manual"
    source_ref: str | None = None
    llm_config: ProjectLlmConfigRequest | None = None


class UpdatePlanRequest(BaseModel):
    plan_md: str


class ApproveRequest(BaseModel):
    feedback: str | None = None


class RollbackRequest(BaseModel):
    checkpoint_id: str


class DeployTargetRequest(BaseModel):
    auth_mode: Literal["ssh_key", "password"] = "ssh_key"
    host: str
    port: int = 22
    user: str
    deploy_path: str
    auth_reference: str | None = None
    ssh_private_key: str | None = None
    ssh_password: str | None = None
    known_hosts: str | None = None
    env_content: str | None = None
    app_url: str | None = None
    health_path: str | None = "/api/health"


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str
    intake_text: str
    intake_source: str
    source_ref: str | None
    status: str
    project_stage: str
    plan_md: str | None
    stack_config: dict[str, Any] = Field(default_factory=dict)
    llm_config: dict[str, Any] = Field(default_factory=default_llm_config)
    execution_contract: dict[str, Any] = Field(default_factory=dict)
    contract_version: int = 0
    decision_log: list[dict[str, Any]] = Field(default_factory=list)
    plan_delta_history: list[dict[str, Any]] = Field(default_factory=list)
    repo_name: str | None
    repo_url: str | None
    repo_clone_url: str | None
    default_branch: str | None
    develop_branch: str | None
    feature_branch: str | None
    repo_ready: bool = False
    container_id: str | None
    port: int | None
    preview_url: str | None
    preview_status: str | None
    preview_metadata: dict[str, Any] = Field(default_factory=dict)
    ci_status: str
    ci_run_id: str | None
    ci_run_url: str | None
    deploy_status: str | None
    deploy_run_url: str | None
    deploy_commit_sha: str | None
    github_environment_name: str | None
    last_workflow_sync_at: str | None
    deploy_target_summary: dict[str, Any] | None
    plan_approved: bool = False
    preview_approved: bool = False
    active_task_id: str | None = None
    task_attempt_count: int = 0
    task_validation_result: dict[str, Any] = Field(default_factory=dict)
    last_escalation: dict[str, Any] = Field(default_factory=dict)
    builder_report: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class ModelCatalogEntry(BaseModel):
    id: str
    label: str


class ProviderModelCatalogResponse(BaseModel):
    provider: str
    configured: bool
    models: list[ModelCatalogEntry] = Field(default_factory=list)
    fetched_at: str | None = None
    error: str | None = None
    cached: bool = False


class PreflightRequest(BaseModel):
    phase: Literal["start", "deploy"]


class PreflightCheckResponse(BaseModel):
    code: str
    status: Literal["ok", "warning", "blocking"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class PreflightResponse(BaseModel):
    phase: Literal["start", "deploy"]
    ok: bool
    blocking: bool
    checks: list[PreflightCheckResponse] = Field(default_factory=list)


class ExecutionContractResponse(BaseModel):
    project_id: str
    contract_version: int = 0
    execution_contract: dict[str, Any] = Field(default_factory=dict)


class DecisionLogResponse(BaseModel):
    project_id: str
    contract_version: int = 0
    decision_log: list[dict[str, Any]] = Field(default_factory=list)
    plan_delta_history: list[dict[str, Any]] = Field(default_factory=list)


class TaskStatusResponse(BaseModel):
    project_id: str
    active_task_id: str | None = None
    active_task: dict[str, Any] | None = None
    completed_tasks: int = 0
    total_tasks: int = 0
    task_attempt_count: int = 0
    task_validation_result: dict[str, Any] = Field(default_factory=dict)
    builder_report: dict[str, Any] = Field(default_factory=dict)
    last_escalation: dict[str, Any] = Field(default_factory=dict)


async def _get_required_project(project_id: str) -> dict[str, Any]:
    project = await runner_get_runtime_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/llm/providers", response_model=list[ProviderModelCatalogResponse])
async def api_get_llm_provider_catalogs(force_refresh: bool = False) -> Any:
    return await get_all_provider_model_catalogs(force_refresh=force_refresh)


@router.get("/llm/providers/{provider}/models", response_model=ProviderModelCatalogResponse)
async def api_get_llm_provider_models(provider: str, force_refresh: bool = False) -> Any:
    try:
        return await get_provider_model_catalog(provider, force_refresh=force_refresh)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/projects", response_model=ProjectResponse)
async def api_create_project(req: CreateProjectRequest) -> Any:
    intake_text = (req.intake_text or req.description or "").strip()
    if not intake_text:
        raise HTTPException(status_code=422, detail="Either intake_text or description is required")

    project = await create_project(
        str(uuid.uuid4()),
        req.name,
        intake_text,
        intake_source=req.source,
        source_ref=req.source_ref,
        llm_config=normalize_llm_config(req.llm_config.model_dump() if req.llm_config else None),
    )
    return project


@router.get("/projects", response_model=list[ProjectResponse])
async def api_list_projects() -> Any:
    return await get_all_projects()


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def api_get_project(project_id: str) -> Any:
    return await _get_required_project(project_id)


@router.get("/projects/{project_id}/execution-contract", response_model=ExecutionContractResponse)
async def api_get_execution_contract(project_id: str) -> Any:
    project = await _get_required_project(project_id)
    return {
        "project_id": project_id,
        "contract_version": int(project.get("contract_version", 0) or 0),
        "execution_contract": project.get("execution_contract") or {},
    }


@router.get("/projects/{project_id}/decision-log", response_model=DecisionLogResponse)
async def api_get_decision_log(project_id: str) -> Any:
    project = await _get_required_project(project_id)
    return {
        "project_id": project_id,
        "contract_version": int(project.get("contract_version", 0) or 0),
        "decision_log": project.get("decision_log") or [],
        "plan_delta_history": project.get("plan_delta_history") or [],
    }


@router.get("/projects/{project_id}/task-status", response_model=TaskStatusResponse)
async def api_get_task_status(project_id: str) -> Any:
    await _get_required_project(project_id)
    return await runner_get_task_status(project_id)


@router.delete("/projects/{project_id}")
async def api_delete_project(project_id: str) -> dict[str, str]:
    await _get_required_project(project_id)
    await runner_stop(project_id)
    await update_project_stage(project_id, "error")
    await update_project_status(project_id, "deleted")
    return {"status": "deleted", "project_id": project_id}


@router.put("/projects/{project_id}/plan")
async def api_update_plan(project_id: str, req: UpdatePlanRequest) -> dict[str, str]:
    await _get_required_project(project_id)
    await update_project_plan(project_id, req.plan_md)
    return {"status": "updated"}


@router.put("/projects/{project_id}/llm-config", response_model=ProjectResponse)
async def api_update_llm_config(project_id: str, req: ProjectLlmConfigRequest) -> Any:
    await _get_required_project(project_id)
    await update_project_llm_config(project_id, normalize_llm_config(req.model_dump()))
    return await _get_required_project(project_id)


@router.get("/projects/{project_id}/plan")
async def api_get_plan(project_id: str) -> dict[str, str | None]:
    project = await _get_required_project(project_id)
    return {"plan_md": project.get("plan_md")}


@router.post("/projects/{project_id}/start")
async def api_start_project(project_id: str) -> Any:
    await _get_required_project(project_id)
    result = await runner_start(project_id)
    _raise_for_preflight_failure(result)
    return result


@router.post("/projects/{project_id}/approve-plan")
async def api_approve_plan(project_id: str, req: ApproveRequest | None = None) -> dict[str, str]:
    await _get_required_project(project_id)
    return await runner_resume(project_id, approval_type="plan", feedback=req.feedback if req else None)


@router.post("/projects/{project_id}/approve")
async def api_approve_project(project_id: str, req: ApproveRequest | None = None) -> dict[str, str]:
    return await api_approve_plan(project_id, req)


@router.post("/projects/{project_id}/approve-preview")
async def api_approve_preview(project_id: str, req: ApproveRequest | None = None) -> dict[str, str]:
    await _get_required_project(project_id)
    return await runner_resume(project_id, approval_type="preview", feedback=req.feedback if req else None)


@router.post("/projects/{project_id}/stop")
async def api_stop_project(project_id: str) -> dict[str, str]:
    await _get_required_project(project_id)
    return await runner_stop(project_id)


@router.put("/projects/{project_id}/deploy-target")
async def api_update_deploy_target(project_id: str, req: DeployTargetRequest) -> dict[str, str]:
    project = await _get_required_project(project_id)
    deploy_target = preflight_service.normalize_deploy_target(req.model_dump())
    target_checks = preflight_service.validate_deploy_target_shape(deploy_target)
    blocking_checks = [check for check in target_checks if check.status == "blocking"]
    if blocking_checks:
        raise HTTPException(
            status_code=422,
            detail={
                "phase": "deploy",
                "ok": False,
                "blocking": True,
                "checks": [
                    {
                        "code": check.code,
                        "status": check.status,
                        "message": check.message,
                        "details": check.details,
                    }
                    for check in target_checks
                ],
            },
        )
    if project.get("repo_name"):
        await repository_manager.configure_remote_cicd_for_target(project["repo_name"], deploy_target)
    await update_project_deploy_target(project_id, deploy_target)
    return {"status": "configured", "project_id": project_id}


@router.post("/projects/{project_id}/deploy")
async def api_deploy_project(project_id: str) -> Any:
    await _get_required_project(project_id)
    result = await runner_deploy(project_id)
    _raise_for_preflight_failure(result)
    return result


@router.post("/projects/{project_id}/preflight", response_model=PreflightResponse)
async def api_preflight_project(project_id: str, req: PreflightRequest) -> Any:
    await _get_required_project(project_id)
    return await runner_preflight(project_id, req.phase)


@router.post("/projects/{project_id}/preview/restart", response_model=ProjectResponse)
async def api_restart_preview(project_id: str) -> Any:
    await _get_required_project(project_id)
    result = await runner_restart_preview(project_id)
    if result.get("status") not in {"restarted"}:
        raise HTTPException(status_code=409, detail=result)
    return await _get_required_project(project_id)


@router.post("/projects/{project_id}/sync-cicd", response_model=ProjectResponse)
async def api_sync_cicd(project_id: str) -> Any:
    await _get_required_project(project_id)
    await runner_sync_cicd(project_id)
    return await _get_required_project(project_id)


@router.get("/projects/{project_id}/history")
async def api_get_history(project_id: str) -> dict[str, Any]:
    return {"project_id": project_id, "checkpoints": await get_checkpoints(project_id)}


@router.post("/projects/{project_id}/rollback")
async def api_rollback(project_id: str, req: RollbackRequest) -> dict[str, str]:
    project = await _get_required_project(project_id)
    if is_running(project_id):
        raise HTTPException(status_code=409, detail="Cannot rollback while the project is running")

    graph = await _aget_graph()
    config = _make_thread_config(project_id)
    target_state = None
    async for checkpoint in graph.aget_state_history(config):
        checkpoint_id = checkpoint.config.get("configurable", {}).get("checkpoint_id", "")
        if checkpoint_id == req.checkpoint_id:
            target_state = checkpoint
            break

    if target_state is None:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    checkpoint_config = {
        "configurable": {
            "thread_id": config["configurable"]["thread_id"],
            "checkpoint_id": req.checkpoint_id,
        }
    }
    await graph.aupdate_state(checkpoint_config, target_state.values)
    await sync_project_from_state(project_id, target_state.values)
    await update_project_stage(project_id, target_state.values.get("project_stage", project["project_stage"]))
    await update_project_status(project_id, target_state.values.get("status", project["status"]))
    return {"status": "rolled_back", "project_id": project_id}


@router.get("/projects/{project_id}/logs")
async def api_get_logs(project_id: str) -> list[dict[str, Any]]:
    await _get_required_project(project_id)
    return await get_task_logs(project_id)


@router.get("/projects/{project_id}/sessions")
async def api_get_project_sessions(project_id: str) -> list[dict[str, Any]]:
    await _get_required_project(project_id)
    return await get_sessions(project_id)


@router.get("/projects/{project_id}/chat-history")
async def api_get_chat_history(project_id: str) -> list[dict[str, Any]]:
    await _get_required_project(project_id)
    return await get_chat_messages(project_id)


@router.get("/projects/{project_id}/preview-url")
async def api_get_preview_url(project_id: str) -> dict[str, str | None]:
    project = await _get_required_project(project_id)
    return {"preview_url": project.get("preview_url")}


@router.get("/projects/{project_id}/deploy-runs")
async def api_get_project_deploy_runs(project_id: str) -> list[dict[str, Any]]:
    await _get_required_project(project_id)
    return await get_deploy_runs(project_id)


@router.get("/projects/{project_id}/trace")
async def api_get_project_trace(project_id: str) -> list[dict[str, Any]]:
    await _get_required_project(project_id)
    return await get_trace_events(project_id)


def _raise_for_preflight_failure(result: dict[str, Any]) -> None:
    if result.get("status") == "preflight_failed":
        raise HTTPException(status_code=409, detail=result.get("preflight", {}))
