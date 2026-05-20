"""REST API routes for project lifecycle management."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

import io
import json
import zipfile

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from orchestrator.llm.catalog import get_all_provider_model_catalogs, get_provider_model_catalog
from orchestrator.llm.configuration import default_llm_config, normalize_llm_config
from orchestrator.config import settings
from orchestrator.models.project import (
    create_project,
    get_all_projects,
    get_chat_messages,
    get_deploy_runs,
    get_project,
    get_activity_logs,
    get_task_logs,
    get_trace_events,
    list_github_issues,
    update_project,
    update_project_deploy_target,
    update_project_llm_config,
    update_project_plan,
    update_project_preview,
    update_project_stage,
    update_project_status,
)
from orchestrator.models.session import get_sessions
from orchestrator.orchestrator import (
    assign_to_copilot as orch_assign_copilot,
    assign_to_copilot_issue as orch_assign_copilot_issue,
    audit_project as orch_audit_project,
    create_issue_from_prompt as orch_create_issue_from_prompt,
    implement_with_aider as orch_implement_aider,
    resume_project as orch_resume,
    review_pr as orch_review_pr,
    start_project as orch_start,
    sync_issues as orch_sync_issues,
)
from orchestrator.validation.preflight import PreflightResult, PreflightService
from orchestrator.repository.manager import RepositoryManager

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
    repo_url: str | None = None          # GitHub repo URL — new primary field
    intake_text: str | None = None       # kept for backward compat (treated as repo_url if set)
    description: str | None = None
    source: str = "manual"
    source_ref: str | None = None
    llm_config: ProjectLlmConfigRequest | None = None
    figma_urls: list[str] = Field(default_factory=list)


class ReviewPRRequest(BaseModel):
    issue_number: int
    pr_number: int


class UpdatePlanRequest(BaseModel):
    plan_md: str


class ApproveRequest(BaseModel):
    feedback: str | None = None


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
    figma_urls: list[str] = Field(default_factory=list)
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



async def _get_required_project(project_id: str) -> dict[str, Any]:
    project = await get_project(project_id)
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
    # repo_url takes precedence; fall back to intake_text for backward compat
    repo_url = (req.repo_url or req.intake_text or req.description or "").strip()
    if not repo_url:
        raise HTTPException(status_code=422, detail="repo_url is required")

    # Resolve owner/repo eagerly so the project is correctly scoped to the org
    from orchestrator.orchestrator import _parse_repo_url
    github_repo_owner: str | None = None
    github_repo_name: str | None = None
    parsed = _parse_repo_url(repo_url)
    if parsed:
        github_repo_owner, github_repo_name = parsed
    elif "/" not in repo_url:
        # bare repo name — scope to default org
        default_owner = settings.github_default_org or settings.github_owner
        github_repo_owner = default_owner
        github_repo_name = repo_url

    project = await create_project(
        str(uuid.uuid4()),
        req.name,
        repo_url,
        intake_source=req.source,
        source_ref=req.source_ref,
        llm_config=normalize_llm_config(req.llm_config.model_dump() if req.llm_config else None),
        github_repo_owner=github_repo_owner,
        github_repo_name=github_repo_name,
        figma_urls=[u for u in req.figma_urls if u.strip()],
    )
    return project


@router.get("/projects", response_model=list[ProjectResponse])
async def api_list_projects() -> Any:
    return await get_all_projects()


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def api_get_project(project_id: str) -> Any:
    return await _get_required_project(project_id)




@router.delete("/projects/{project_id}")
async def api_delete_project(project_id: str) -> dict[str, str]:
    await _get_required_project(project_id)
    await update_project_stage(project_id, "error")
    await update_project_status(project_id, "deleted")
    return {"status": "deleted", "project_id": project_id}


@router.get("/projects/{project_id}/issues")
async def api_get_issues(project_id: str) -> list[dict[str, Any]]:
    await _get_required_project(project_id)
    return await list_github_issues(project_id)


@router.post("/projects/{project_id}/sync-issues")
async def api_sync_issues(project_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    await _get_required_project(project_id)
    background_tasks.add_task(orch_sync_issues, project_id)
    return {"status": "syncing", "project_id": project_id}


@router.post("/projects/{project_id}/audit")
async def api_audit_project(project_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    await _get_required_project(project_id)
    background_tasks.add_task(orch_audit_project, project_id)
    return {"status": "auditing", "project_id": project_id}


@router.post("/projects/{project_id}/build-check")
async def api_run_build_check(project_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    project = await _get_required_project(project_id)
    owner = project.get("github_repo_owner") or ""
    repo = project.get("github_repo_name") or ""
    if not owner or not repo:
        raise HTTPException(status_code=422, detail="Project has no GitHub repo configured")
    default_branch = project.get("default_branch") or "main"
    from orchestrator.build_check import run_project_build_check
    background_tasks.add_task(run_project_build_check, project_id, owner, repo, default_branch)
    return {"status": "started", "project_id": project_id}


@router.post("/projects/{project_id}/figma-file")
async def api_upload_figma_file(project_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    """Accept a .fig file, extract its document.json, summarise it, store as design context."""
    await _get_required_project(project_id)

    if not (file.filename or "").lower().endswith(".fig"):
        raise HTTPException(status_code=400, detail="Only .fig files are accepted")

    data = await file.read()

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            # .fig files contain document.json at the root
            doc_name = next((n for n in names if n.endswith("document.json")), None)
            if not doc_name:
                raise HTTPException(status_code=422, detail=f".fig file has no document.json (found: {names[:5]})")
            with zf.open(doc_name) as f:
                doc = json.load(f)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=422, detail="File is not a valid .fig archive")

    from orchestrator.orchestrator import _summarise_figma_node
    summary = _summarise_figma_node(doc)

    await update_project(project_id, figma_file_context=summary)

    return {"status": "ok", "chars": len(summary), "filename": file.filename}


@router.post("/projects/{project_id}/assign-copilot")
async def api_assign_copilot(project_id: str) -> dict:
    await _get_required_project(project_id)
    return await orch_assign_copilot(project_id)


@router.post("/projects/{project_id}/issues/{issue_number}/assign-copilot")
async def api_assign_copilot_issue(project_id: str, issue_number: int) -> dict:
    await _get_required_project(project_id)
    return await orch_assign_copilot_issue(project_id, issue_number)


@router.post("/projects/{project_id}/issues/{issue_number}/implement")
async def api_implement_aider(
    project_id: str, issue_number: int, background_tasks: BackgroundTasks
) -> dict[str, str]:
    await _get_required_project(project_id)
    background_tasks.add_task(orch_implement_aider, project_id, issue_number)
    return {"status": "started", "issue_number": str(issue_number)}


class CreateIssueFromPromptRequest(BaseModel):
    prompt: str


@router.post("/projects/{project_id}/issues/create-from-prompt")
async def api_create_issue_from_prompt(
    project_id: str, req: CreateIssueFromPromptRequest, background_tasks: BackgroundTasks
) -> dict[str, str]:
    await _get_required_project(project_id)
    background_tasks.add_task(orch_create_issue_from_prompt, project_id, req.prompt)
    return {"status": "creating"}


@router.post("/projects/{project_id}/review-pr")
async def api_review_pr(
    project_id: str, req: ReviewPRRequest, background_tasks: BackgroundTasks
) -> dict[str, str]:
    await _get_required_project(project_id)
    background_tasks.add_task(orch_review_pr, project_id, req.issue_number, req.pr_number)
    return {"status": "reviewing", "project_id": project_id}


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
async def api_start_project(project_id: str, background_tasks: BackgroundTasks) -> Any:
    project = await _get_required_project(project_id)
    # Run preflight (LLM config check only in GitHub-native mode)
    result = await preflight_service.run("start", project=project)
    _raise_for_preflight_failure(result)
    background_tasks.add_task(orch_start, project_id)
    return {"status": "started", "project_id": project_id}


@router.post("/projects/{project_id}/approve-plan")
async def api_approve_plan(
    project_id: str, background_tasks: BackgroundTasks, req: ApproveRequest | None = None
) -> dict[str, str]:
    await _get_required_project(project_id)
    background_tasks.add_task(orch_resume, project_id, "plan", True)
    return {"status": "resuming", "project_id": project_id}


@router.post("/projects/{project_id}/approve")
async def api_approve_project(
    project_id: str, background_tasks: BackgroundTasks, req: ApproveRequest | None = None
) -> dict[str, str]:
    return await api_approve_plan(project_id, background_tasks, req)


@router.post("/projects/{project_id}/stop")
async def api_stop_project(project_id: str) -> dict[str, str]:
    await _get_required_project(project_id)
    await update_project_status(project_id, "stopped")
    await update_project_stage(project_id, "stopped")
    return {"status": "stopped", "project_id": project_id}


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


@router.post("/projects/{project_id}/preflight", response_model=PreflightResponse)
async def api_preflight_project(project_id: str, req: PreflightRequest) -> Any:
    project = await _get_required_project(project_id)
    return await preflight_service.run(req.phase, project=project)


@router.get("/projects/{project_id}/logs")
async def api_get_logs(project_id: str) -> list[dict[str, Any]]:
    await _get_required_project(project_id)
    activity = await get_activity_logs(project_id)
    if activity:
        return activity
    # fall back to legacy task_logs for old projects
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


@router.post("/projects/{project_id}/preview/restart")
async def api_restart_preview(project_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    project = await _get_required_project(project_id)
    owner = project.get("github_repo_owner") or ""
    repo = project.get("github_repo_name") or ""
    if not owner or not repo:
        raise HTTPException(status_code=422, detail="Project has no GitHub repo configured")
    default_branch = project.get("default_branch") or "main"
    background_tasks.add_task(_run_preview_and_save, project_id, owner, repo, default_branch)
    return {"status": "started", "project_id": project_id}


async def _run_preview_and_save(project_id: str, owner: str, repo: str, default_branch: str = "main") -> None:
    from orchestrator.preview import run_preview_locally
    from orchestrator.api.websocket import emit_status_update, emit_error
    preview_url = await run_preview_locally(project_id, owner, repo, default_branch)
    if not preview_url:
        await emit_error(project_id, "Preview build failed — check that the repo builds successfully")
        return
    await update_project_preview(project_id, preview_url, "ready")
    project = await get_project(project_id)
    if project:
        await emit_status_update(
            project_id,
            status=project.get("status", "preview"),
            stage=project.get("project_stage", "building"),
            progress={},
            preview_url=preview_url,
        )


def _raise_for_preflight_failure(result: PreflightResult) -> None:
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.to_dict())
