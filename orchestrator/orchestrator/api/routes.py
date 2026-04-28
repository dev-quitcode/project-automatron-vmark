"""REST API routes for project lifecycle management."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

import io
import json
import re
import zipfile

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Response, UploadFile
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
    update_project_deployment,
    update_project_llm_config,
    update_project_plan,
    update_project_stage,
    update_project_status,
)
from orchestrator.models.session import get_sessions
from orchestrator.orchestrator import (
    assign_to_copilot as orch_assign_copilot,
    assign_to_copilot_issue as orch_assign_copilot_issue,
    audit_project as orch_audit_project,
    orch_deploy,
    orch_generate_deploy_artifacts,
    orch_plan_deployment,
    orch_sync_deploy,
    resume_project as orch_resume,
    review_pr as orch_review_pr,
    start_project as orch_start,
    sync_issues as orch_sync_issues,
)
from orchestrator.deployment_v2 import get_strategy
from orchestrator.deployment_v2.kamal.config import KamalConfig
from orchestrator.deployment_v2.profile import DeploymentProfile, DeploymentSecrets
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


class DeployTargetConfig(BaseModel):
    """Persisted deployment configuration (no secret values)."""

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


class DeployTargetSecretsBody(BaseModel):
    """Write-only secrets, immediately upserted to GitHub Environment Secrets."""

    ssh_private_key: str
    registry_password: str
    secret_env_values: dict[str, str] = Field(default_factory=dict)


class DeployTargetRequest(BaseModel):
    config: DeployTargetConfig
    secrets: DeployTargetSecretsBody


class GenerateArtifactsRequest(BaseModel):
    pass


class DeployRequest(BaseModel):
    pass


class RollbackRequest(BaseModel):
    rollback_to: str | None = None


class DeployPreflightRequest(BaseModel):
    phase: Literal["generate_artifacts", "setup", "deploy", "health_verify"] = "deploy"


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
async def api_update_deploy_target(project_id: str, req: DeployTargetRequest) -> dict[str, Any]:
    """Validate config, persist it, and write-through secrets to GitHub.

    Secret values are scrubbed from memory after upsert. Persisted state
    contains only the secret names.
    """
    project = await _get_required_project(project_id)
    repo_name = project.get("repo_name") or project.get("github_repo_name")
    if not repo_name:
        raise HTTPException(status_code=409, detail="Project has no GitHub repo configured")

    strategy = get_strategy(req.config.strategy)
    config_dict = req.config.model_dump()
    validation = strategy.validate_config(config_dict)
    blocking = [c for c in validation if c.status == "blocking"]
    if blocking:
        raise HTTPException(
            status_code=422,
            detail={
                "phase": "deploy",
                "ok": False,
                "blocking": True,
                "checks": [
                    {"code": c.code, "status": c.status, "message": c.message, "details": c.details}
                    for c in validation
                ],
            },
        )

    # Merge config into the existing detector profile so we keep framework /
    # next_output / health_route_files intact.
    existing_profile = project.get("deployment_profile") or {}
    if existing_profile:
        profile = DeploymentProfile.from_dict(existing_profile)
    else:
        profile = strategy.detect_requirements(project)
    profile = strategy.merge_config_into_profile(profile, config_dict)
    profile.environment_name = project.get("github_environment_name") or "production"

    secrets = DeploymentSecrets(
        ssh_private_key=req.secrets.ssh_private_key,
        registry_password=req.secrets.registry_password,
        secret_env_values=dict(req.secrets.secret_env_values),
    )
    secret_payload = strategy.secrets_payload(profile, secrets)

    secret_names = await repository_manager.configure_remote_deployment_v2(
        repo_name,
        secret_payload,
        environment_name=profile.environment_name,
    )
    secrets.scrub()
    secret_payload.clear()

    await update_project_deploy_target(project_id, config_dict)
    await update_project_deployment(
        project_id,
        deployment_strategy=req.config.strategy,
        deployment_profile=profile.to_dict(),
        deployment_secret_names=secret_names,
        auto_deploy_on_main=req.config.auto_deploy_on_main,
        artifacts_push_mode=req.config.artifacts_push_mode,
    )
    await update_project_stage(project_id, "deploy_target_configured")
    return {
        "status": "configured",
        "project_id": project_id,
        "secret_names": secret_names,
        "stage": "deploy_target_configured",
    }


@router.post("/projects/{project_id}/generate-deploy-artifacts")
async def api_generate_deploy_artifacts(
    project_id: str,
    req: GenerateArtifactsRequest | None = None,
) -> dict[str, Any]:
    await _get_required_project(project_id)
    fingerprint = await orch_generate_deploy_artifacts(project_id)
    return {
        "status": "generated",
        "project_id": project_id,
        "fingerprint": fingerprint,
    }


@router.post("/projects/{project_id}/deploy-preflight", response_model=PreflightResponse)
async def api_deploy_preflight(project_id: str, req: DeployPreflightRequest) -> Any:
    project = await _get_required_project(project_id)
    return await preflight_service.run_v2(project, req.phase)


@router.post("/projects/{project_id}/deploy")
async def api_deploy(project_id: str, req: DeployRequest | None = None) -> dict[str, Any]:
    await _get_required_project(project_id)
    result = await orch_deploy(project_id, action="deploy")
    return {"status": "dispatched", "project_id": project_id, **result}


@router.post("/projects/{project_id}/setup")
async def api_setup(project_id: str, req: DeployRequest | None = None) -> dict[str, Any]:
    await _get_required_project(project_id)
    result = await orch_deploy(project_id, action="setup")
    return {"status": "dispatched", "project_id": project_id, **result}


@router.post("/projects/{project_id}/rollback")
async def api_rollback(project_id: str, req: RollbackRequest) -> dict[str, Any]:
    await _get_required_project(project_id)
    try:
        result = await orch_deploy(project_id, action="rollback", rollback_to=req.rollback_to)
    except RuntimeError as exc:
        if str(exc) == "rollback_no_previous_deploy":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rollback_no_previous_deploy",
                    "message": "No previous successful deploy is recorded for this project",
                },
            )
        raise
    return {"status": "dispatched", "project_id": project_id, **result}


@router.get("/projects/{project_id}/deploy-status")
async def api_deploy_status(project_id: str) -> dict[str, Any]:
    await _get_required_project(project_id)
    return await orch_sync_deploy(project_id)


_LOG_REDACT_PATTERN = re.compile(
    r"(KAMAL_REGISTRY_PASSWORD|KAMAL_SSH_PRIVATE_KEY)=([^\r\n]*)",
    re.IGNORECASE,
)


@router.get("/projects/{project_id}/deploy-logs")
async def api_deploy_logs(project_id: str) -> Response:
    project = await _get_required_project(project_id)
    repo_name = project.get("repo_name") or project.get("github_repo_name")
    run_id = project.get("last_deploy_run_id")
    if not repo_name or not run_id:
        raise HTTPException(status_code=404, detail="No deploy run available")

    from orchestrator.github_actions.manager import GitHubActionsManager

    actions = GitHubActionsManager()
    try:
        zip_bytes = await actions.download_workflow_logs(repo_name, run_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch logs: {exc}") from exc

    secret_names = list(project.get("deployment_secret_names") or [])
    redacted = _redact_log_secrets(zip_bytes, secret_names)
    return Response(
        content=redacted,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="deploy-{run_id}.zip"'},
    )


def _redact_log_secrets(zip_bytes: bytes, secret_names: list[str]) -> bytes:
    """Walk a workflow logs zip and mask `SECRET=value` lines for known names."""
    if not zip_bytes:
        return zip_bytes
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zin, zipfile.ZipFile(
            output, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            patterns = [_LOG_REDACT_PATTERN] + [
                re.compile(rf"\b{re.escape(name)}=([^\r\n]*)", re.IGNORECASE)
                for name in secret_names
            ]
            for info in zin.infolist():
                with zin.open(info) as src:
                    raw = src.read()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    zout.writestr(info, raw)
                    continue
                for pattern in patterns:
                    text = pattern.sub(lambda m: f"{m.group(0).split('=', 1)[0]}=***", text)
                zout.writestr(info, text.encode("utf-8"))
    except zipfile.BadZipFile:
        return zip_bytes
    return output.getvalue()


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


def _raise_for_preflight_failure(result: PreflightResult) -> None:
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.to_dict())
