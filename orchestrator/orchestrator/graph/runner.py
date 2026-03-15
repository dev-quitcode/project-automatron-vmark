"""Graph runner — manages LangGraph execution sessions and deploys."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from orchestrator.api.websocket import (
    emit_human_required,
    emit_plan_updated,
    emit_status_update,
)
from orchestrator.config import settings
from orchestrator.deployment.manager import DeploymentManager
from orchestrator.docker_engine.manager import ContainerManager
from orchestrator.docker_engine.port_allocator import PortAllocator
from orchestrator.execution_contract import default_task_status_payload
from orchestrator.graph.graph import compile_graph
from orchestrator.llm.configuration import default_llm_config, normalize_llm_config
from orchestrator.models.project import (
    get_project,
    record_approval,
    sync_project_from_state,
    update_project_cicd,
    update_project_deploy_status,
    update_project_preview,
    update_project_stage,
    update_project_status,
    upsert_deploy_run,
)
from orchestrator.models.session import create_session, end_session
from orchestrator.observability import trace_event
from orchestrator.repository.manager import RepositoryManager
from orchestrator.validation.preflight import PreflightService
from orchestrator.validation.workspace import validate_workspace_contract_async

logger = logging.getLogger(__name__)

_active_runs: dict[str, asyncio.Task] = {}
_compiled_graph = None
repository_manager = RepositoryManager()
container_manager = ContainerManager()
port_allocator = PortAllocator(start=settings.port_range_start, end=settings.port_range_end)
preflight_service = PreflightService(
    container_manager=container_manager,
    repository_manager=repository_manager,
)
# Retained for manual fallback deploy mode outside the primary GitHub Actions path.
manual_deployment_manager = DeploymentManager()


def _get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        raise RuntimeError("_get_graph() must not be used synchronously")
    return _compiled_graph


async def _aget_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = await compile_graph()
    return _compiled_graph


def _make_thread_config(project_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"automatron:{project_id}"}}


def _is_interrupt_result(result: Any) -> bool:
    return isinstance(result, dict) and "__interrupt__" in result


def _status_from_stage(stage: str) -> str:
    mapping = {
        "intake": "pending",
        "planning": "planning",
        "awaiting_plan_approval": "planning",
        "repo_preparing": "planning",
        "scaffolding": "building",
        "building": "building",
        "validating": "validating",
        "awaiting_architect_delta": "building",
        "awaiting_preview_approval": "preview",
        "ready_for_deploy": "ready_for_deploy",
        "deploying": "deploying",
        "deployed": "deployed",
        "frozen": "frozen",
        "error": "error",
    }
    return mapping.get(stage, "pending")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def sync_cicd_status(project_id: str) -> dict[str, str]:
    project = await get_project(project_id)
    if not project:
        return {"status": "not_found", "project_id": project_id}
    if not project.get("repo_name"):
        return {"status": "not_configured", "project_id": project_id}

    try:
        result = await repository_manager.sync_remote_cicd_status(
            project["repo_name"],
            feature_branch=project.get("feature_branch") or "",
            develop_branch=project.get("develop_branch") or "develop",
            default_branch=project.get("default_branch") or "main",
        )
    except Exception:
        logger.exception("Failed to sync CI/CD status for %s", project_id)
        return {"status": "failed", "project_id": project_id}

    ci = result["ci"]
    deploy = result["deploy"]
    ci_status = ci.status if ci.run_id else project.get("ci_status") or "not_configured"
    deploy_status = deploy.status if deploy.run_id else project.get("deploy_status") or "not_configured"

    await update_project_cicd(
        project_id,
        ci_status=ci_status,
        ci_run_id=ci.run_id if ci.run_id else project.get("ci_run_id"),
        ci_run_url=ci.run_url if ci.run_url else project.get("ci_run_url"),
        deploy_status=deploy_status,
        deploy_run_url=deploy.run_url if deploy.run_url else project.get("deploy_run_url"),
        deploy_commit_sha=deploy.head_sha if deploy.head_sha else project.get("deploy_commit_sha"),
        github_environment_name=project.get("github_environment_name") or settings.github_environment_name,
        last_workflow_sync_at=_now(),
    )

    if deploy.run_id:
        deploy_summary = {
            "provider": "github_actions",
            "run_url": deploy.run_url,
            "commit_sha": deploy.head_sha,
        }
        await upsert_deploy_run(
            f"github-{deploy.run_id}",
            project_id,
            deploy.status,
            project.get("default_branch") or "main",
            f"GitHub Actions {deploy.status}",
            summary=deploy_summary,
            deployed_at=deploy.updated_at if deploy.status == "deployed" else None,
        )

    if deploy_status == "deployed":
        await update_project_stage(project_id, "deployed")
        await update_project_status(project_id, "deployed")
        await update_project_deploy_status(
            project_id,
            "deployed",
            last_deploy_at=deploy.updated_at or _now(),
            last_deploy_run_id=deploy.run_id,
            deploy_run_url=deploy.run_url,
            deploy_commit_sha=deploy.head_sha,
        )
        await emit_status_update(project_id, status="deployed", stage="deployed", progress={})
    elif deploy_status in {"queued", "running"}:
        await update_project_stage(project_id, "deploying")
        await update_project_status(project_id, "deploying")
        await update_project_deploy_status(
            project_id,
            deploy_status,
            last_deploy_run_id=deploy.run_id,
            deploy_run_url=deploy.run_url,
            deploy_commit_sha=deploy.head_sha,
        )
        await emit_status_update(project_id, status="deploying", stage="deploying", progress={})
    elif deploy_status == "failed":
        await update_project_stage(project_id, "error")
        await update_project_status(project_id, "error")
        await update_project_deploy_status(
            project_id,
            "failed",
            last_deploy_run_id=deploy.run_id,
            deploy_run_url=deploy.run_url,
            deploy_commit_sha=deploy.head_sha,
        )
        await emit_status_update(project_id, status="error", stage="error", progress={})

    return {
        "status": "synced",
        "project_id": project_id,
        "ci_status": ci_status,
        "deploy_status": deploy_status,
    }


async def run_preflight(project_id: str, phase: str) -> dict[str, Any]:
    project = await get_project(project_id)
    if not project:
        return {
            "phase": phase,
            "ok": False,
            "blocking": True,
            "checks": [
                {
                    "code": "project_not_found",
                    "status": "blocking",
                    "message": "Project not found",
                    "details": {"project_id": project_id},
                }
            ],
        }
    normalized_phase = "deploy" if phase == "deploy" else "start"
    return (await preflight_service.run(normalized_phase, project=project)).to_dict()


async def get_task_status(project_id: str) -> dict[str, Any]:
    project = await get_runtime_project(project_id)
    if not project:
        return {
            "project_id": project_id,
            "active_task_id": None,
            "active_task": None,
            "completed_tasks": 0,
            "total_tasks": 0,
            "task_attempt_count": 0,
            "task_validation_result": {},
            "builder_report": {},
            "last_escalation": {},
        }

    payload = default_task_status_payload(
        project.get("execution_contract") or {},
        project.get("active_task_id"),
    )
    payload.update(
        {
            "project_id": project_id,
            "task_attempt_count": int(project.get("task_attempt_count", 0) or 0),
            "task_validation_result": project.get("task_validation_result") or {},
            "builder_report": project.get("builder_report") or {},
            "last_escalation": project.get("last_escalation") or {},
        }
    )
    return payload


def _overlay_project_with_runtime_state(project: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    merged = dict(project)
    field_map = {
        "plan_md": "plan_md",
        "stack_config": "stack_config",
        "llm_config": "llm_config",
        "execution_contract": "execution_contract",
        "contract_version": "contract_version",
        "decision_log": "decision_log",
        "plan_delta_history": "plan_delta_history",
        "project_stage": "project_stage",
        "status": "status",
        "active_task_id": "active_task_id",
        "task_attempt_count": "task_attempt_count",
        "task_validation_result": "task_validation_result",
        "last_escalation": "last_escalation",
        "builder_report": "builder_report",
        "container_id": "container_id",
        "repo_name": "repo_name",
        "repo_url": "repo_url",
        "repo_clone_url": "repo_clone_url",
        "default_branch": "default_branch",
        "develop_branch": "develop_branch",
        "feature_branch": "feature_branch",
        "repo_ready": "repo_ready",
        "preview_url": "preview_url",
        "preview_status": "preview_status",
        "preview_metadata": "preview_metadata",
        "ci_status": "ci_status",
        "ci_run_id": "ci_run_id",
        "ci_run_url": "ci_run_url",
        "deploy_status": "deploy_status",
        "deploy_run_url": "deploy_run_url",
        "deploy_commit_sha": "deploy_commit_sha",
        "github_environment_name": "github_environment_name",
        "last_workflow_sync_at": "last_workflow_sync_at",
        "plan_approved": "plan_approved",
        "preview_approved": "preview_approved",
    }
    for state_key, project_key in field_map.items():
        if state_key in state:
            merged[project_key] = state.get(state_key)
    if "container_port" in state:
        merged["port"] = state.get("container_port")
        merged["preview_port"] = state.get("container_port")
    return merged


async def get_runtime_project(project_id: str) -> dict[str, Any] | None:
    project = await get_project(project_id)
    if not project:
        return None

    try:
        graph = await _aget_graph()
        snapshot = await graph.aget_state(_make_thread_config(project_id))
    except Exception:
        return project

    values = snapshot.values if snapshot and snapshot.values else {}
    if not values:
        return project
    return _overlay_project_with_runtime_state(project, values)


async def _cleanup_run_resources(project_id: str) -> None:
    project = await get_project(project_id)
    if not project:
        return

    container_id = project.get("container_id")
    if container_id:
        await container_manager.remove_container(container_id)
    await port_allocator.release(project_id)
    await sync_project_from_state(
        project_id,
        {
            "container_id": "",
            "container_port": 0,
            "preview_url": "",
            "preview_status": "pending",
            "preview_metadata": {
                "cleanup_reason": "graph_run_failed",
                "cleaned_at": _now(),
            },
        },
    )


async def _persist_and_emit(project_id: str, graph: Any, config: dict[str, Any]) -> dict[str, Any]:
    snapshot = await graph.aget_state(config)
    values = snapshot.values if snapshot and snapshot.values else {}
    if values:
        if "status" not in values and values.get("project_stage"):
            values["status"] = _status_from_stage(values["project_stage"])
        await sync_project_from_state(project_id, values)
        await emit_status_update(
            project_id,
            status=values.get("status", "pending"),
            stage=values.get("project_stage", "intake"),
            progress={
                "total": values.get("total_tasks", 0),
                "completed": values.get("completed_tasks", 0),
            },
            preview_url=values.get("preview_url"),
        )
        if values.get("plan_md"):
            await emit_plan_updated(project_id, values["plan_md"])
        if values.get("requires_human"):
            await emit_human_required(
                project_id,
                values.get("human_intervention_reason", "Review required"),
                stage=values.get("project_stage"),
            )
        await trace_event(
            project_id,
            "orchestrator",
            "graph.state.persisted",
            {
                "status": values.get("status", "pending"),
                "project_stage": values.get("project_stage", "intake"),
                "active_task_id": values.get("active_task_id", ""),
                "current_task_index": values.get("current_task_index", -1),
                "builder_status": values.get("builder_status", ""),
                "validation_gate_status": values.get("validation_gate_status", ""),
                "requires_human": values.get("requires_human", False),
            },
            session_id=values.get("session_id"),
            stage=values.get("project_stage"),
        )
    return values


async def _run_graph(
    project_id: str,
    *,
    initial_state: dict[str, Any] | None = None,
    resume_payload: dict[str, Any] | None = None,
    resume_state_patch: dict[str, Any] | None = None,
) -> None:
    graph = await _aget_graph()
    config = _make_thread_config(project_id)
    session_id = str(uuid.uuid4())

    try:
        phase = "PLANNING" if initial_state else "RESUME"
        await create_session(session_id, project_id, config["configurable"]["thread_id"], phase)
        await trace_event(
            project_id,
            "orchestrator",
            "graph.run.started",
            {
                "mode": "initial" if initial_state is not None else "resume",
                "phase": phase,
            },
            session_id=session_id,
            stage=(initial_state or resume_state_patch or {}).get("project_stage"),
        )

        if initial_state is not None:
            initial_state["session_id"] = session_id
            await graph.ainvoke(initial_state, config)
        else:
            state_patch = {"session_id": session_id}
            if resume_state_patch:
                state_patch.update(resume_state_patch)
            await graph.aupdate_state(config, state_patch)
            await graph.ainvoke(Command(resume=resume_payload or {"approved": True}), config)

        values = await _persist_and_emit(project_id, graph, config)
        stage = values.get("project_stage", "pending")
        if stage == "ready_for_deploy":
            await update_project_stage(project_id, "ready_for_deploy")
            await update_project_status(project_id, "ready_for_deploy")
        elif values.get("requires_human"):
            await update_project_status(project_id, _status_from_stage(stage))
        elif stage == "frozen":
            await update_project_status(project_id, "frozen")
        await trace_event(
            project_id,
            "orchestrator",
            "graph.run.completed",
            {"project_stage": stage, "status": values.get("status", "")},
            session_id=session_id,
            stage=stage,
        )

    except asyncio.CancelledError:
        logger.info("Graph run cancelled for %s", project_id)
        await update_project_status(project_id, "paused")
        await emit_status_update(project_id, status="paused", stage="intake", progress={})
        await trace_event(
            project_id,
            "orchestrator",
            "graph.run.cancelled",
            {"status": "paused"},
            session_id=session_id,
            stage="paused",
        )
    except Exception:
        logger.exception("Graph run failed for %s", project_id)
        await _cleanup_run_resources(project_id)
        await update_project_stage(project_id, "error")
        await update_project_status(project_id, "error")
        await emit_status_update(project_id, status="error", stage="error", progress={})
        await trace_event(
            project_id,
            "orchestrator",
            "graph.run.failed",
            {"status": "error"},
            session_id=session_id,
            stage="error",
        )
    finally:
        await end_session(session_id)
        _active_runs.pop(project_id, None)


async def start_project(project_id: str) -> dict[str, str]:
    if project_id in _active_runs:
        return {"status": "already_running", "project_id": project_id}

    project = await get_project(project_id)
    if not project:
        return {"status": "not_found", "project_id": project_id}

    preflight = await preflight_service.run("start", project=project)
    if preflight.blocking:
        await trace_event(
            project_id,
            "orchestrator",
            "preflight.failed",
            preflight.to_dict(),
            stage="planning",
        )
        return {
            "status": "preflight_failed",
            "project_id": project_id,
            "preflight": preflight.to_dict(),
        }

    latest_checkpoint = await get_latest_checkpoint_summary(project_id)
    should_resume_existing_run = _should_resume_existing_run(project, latest_checkpoint)

    if should_resume_existing_run:
        project_stage = _normalize_resume_stage(project, latest_checkpoint)

        await update_project_stage(project_id, project_stage)
        await update_project_status(project_id, _status_from_stage(project_stage))
        resume_state_patch = _build_resume_state_patch(project)
        task = asyncio.create_task(
            _run_graph(
                project_id,
                resume_payload={"approved": True, "retry": True},
                resume_state_patch=resume_state_patch,
            )
        )
        _active_runs[project_id] = task
        await trace_event(
            project_id,
            "orchestrator",
            "project.started",
            {"mode": "resume", "project_stage": project_stage},
            stage=project_stage,
        )
        return {"status": "resumed", "project_id": project_id}

    initial_state: dict[str, Any] = {
        "project_id": project_id,
        "project_name": project["name"],
        "intake_text": project.get("intake_text", ""),
        "intake_source": project.get("intake_source", "manual"),
        "source_ref": project.get("source_ref") or "",
        "plan_md": project.get("plan_md", ""),
        "stack_config": project.get("stack_config", {}),
        "llm_config": normalize_llm_config(project.get("llm_config") or default_llm_config()),
        "execution_contract": project.get("execution_contract", {}),
        "contract_version": int(project.get("contract_version", 0) or 0),
        "decision_log": project.get("decision_log", []),
        "plan_delta_history": project.get("plan_delta_history", []),
        "current_task_index": 0,
        "active_task_id": project.get("active_task_id") or "",
        "current_task_text": "",
        "total_tasks": 0,
        "completed_tasks": 0,
        "task_attempt_count": int(project.get("task_attempt_count", 0) or 0),
        "task_validation_result": project.get("task_validation_result", {}),
        "last_escalation": project.get("last_escalation", {}),
        "builder_report": project.get("builder_report", {}),
        "messages": [HumanMessage(content=project.get("intake_text", ""))],
        "builder_status": "",
        "builder_output": "",
        "builder_error_detail": "",
        "escalation_count": 0,
        "escalation_history": [],
        "container_id": project.get("container_id") or "",
        "container_port": project.get("port") or 0,
        "repo_name": project.get("repo_name") or "",
        "repo_url": project.get("repo_url") or "",
        "repo_clone_url": project.get("repo_clone_url") or "",
        "default_branch": project.get("default_branch") or "main",
        "develop_branch": project.get("develop_branch") or "develop",
        "feature_branch": project.get("feature_branch") or repository_manager.create_feature_branch_name(project["name"]),
        "repo_ready": bool(project.get("repo_ready", False)),
        "preview_url": project.get("preview_url") or "",
        "preview_status": project.get("preview_status") or "pending",
        "preview_metadata": project.get("preview_metadata", {}),
        "deploy_target": project.get("deploy_target", {}),
        "project_stage": "planning",
        "status": "planning",
        "requires_human": False,
        "human_intervention_reason": "",
        "plan_approved": bool(project.get("plan_approved", False)),
        "preview_approved": bool(project.get("preview_approved", False)),
    }

    await update_project_stage(project_id, "planning")
    await update_project_status(project_id, "planning")
    task = asyncio.create_task(_run_graph(project_id, initial_state=initial_state))
    _active_runs[project_id] = task
    await trace_event(
        project_id,
        "orchestrator",
        "project.started",
        {"mode": "initial", "project_stage": "planning"},
        stage="planning",
    )
    return {"status": "started", "project_id": project_id}


def _normalize_resume_stage(
    project: dict[str, Any],
    checkpoint_summary: dict[str, Any] | None = None,
) -> str:
    if checkpoint_summary:
        checkpoint_stage = checkpoint_summary.get("project_stage") or ""
        if checkpoint_stage and checkpoint_stage not in {"error", "intake"}:
            return checkpoint_stage

    project_stage = project.get("project_stage") or "planning"

    if project.get("preview_approved"):
        return "ready_for_deploy"
    if project.get("preview_url"):
        return "awaiting_preview_approval"
    if project.get("container_id"):
        return "building"
    if project.get("repo_ready"):
        return "repo_preparing"
    if project.get("plan_approved"):
        return "repo_preparing"
    if project.get("plan_md"):
        return "awaiting_plan_approval"

    if project_stage in {"error", "intake"}:
        return "planning"

    return project_stage


async def resume_project(
    project_id: str,
    *,
    approval_type: str,
    feedback: str | None = None,
) -> dict[str, str]:
    if project_id in _active_runs:
        return {"status": "already_running", "project_id": project_id}

    project = await get_project(project_id)
    if not project:
        return {"status": "not_found", "project_id": project_id}

    await record_approval(project_id, approval_type, True, feedback=feedback)
    await trace_event(
        project_id,
        "operator",
        "approval.recorded",
        {"approval_type": approval_type, "feedback": feedback, "approved": True},
        stage=project.get("project_stage"),
    )
    payload = {"approved": True, "feedback": feedback, "approval_type": approval_type}
    resume_state_patch = _build_resume_state_patch(project)
    task = asyncio.create_task(
        _run_graph(
            project_id,
            resume_payload=payload,
            resume_state_patch=resume_state_patch,
        )
    )
    _active_runs[project_id] = task
    return {"status": "resumed", "project_id": project_id, "approval_type": approval_type}


async def deploy_project(project_id: str) -> dict[str, str]:
    project = await get_project(project_id)
    if not project:
        return {"status": "not_found", "project_id": project_id}
    if project.get("project_stage") != "ready_for_deploy":
        return {"status": "invalid_stage", "project_id": project_id}

    preflight = await preflight_service.run("deploy", project=project)
    if preflight.blocking:
        await trace_event(
            project_id,
            "orchestrator",
            "preflight.failed",
            preflight.to_dict(),
            stage="deploying",
        )
        return {
            "status": "preflight_failed",
            "project_id": project_id,
            "preflight": preflight.to_dict(),
        }

    await update_project_stage(project_id, "deploying")
    await update_project_status(project_id, "deploying")
    await update_project_deploy_status(project_id, "queued")
    await update_project_cicd(
        project_id,
        deploy_status="queued",
        github_environment_name=project.get("github_environment_name") or settings.github_environment_name,
        last_workflow_sync_at=_now(),
    )
    await emit_status_update(project_id, status="deploying", stage="deploying", progress={})

    try:
        await trace_event(
            project_id,
            "orchestrator",
            "deploy.started",
            {
                "repo_name": project.get("repo_name"),
                "develop_branch": project.get("develop_branch") or "develop",
                "default_branch": project.get("default_branch") or "main",
            },
            stage="deploying",
        )
        deploy_sha = repository_manager.merge_branch(
            project_id,
            project.get("develop_branch") or "develop",
            project.get("default_branch") or "main",
            "chore: promote develop to main for deploy",
        )
        await update_project_deploy_status(
            project_id,
            "queued",
            deploy_commit_sha=deploy_sha,
        )
        await update_project_cicd(project_id, deploy_commit_sha=deploy_sha, deploy_status="queued")
        await asyncio.sleep(2)
        sync_result = await sync_cicd_status(project_id)
        await trace_event(
            project_id,
            "orchestrator",
            "deploy.queued",
            {"deploy_commit_sha": deploy_sha, "sync_result": sync_result},
            stage="deploying",
        )
        return {
            "status": sync_result.get("deploy_status", "queued"),
            "project_id": project_id,
        }
    except Exception as exc:
        logger.exception("Deploy failed for %s", project_id)
        await update_project_stage(project_id, "error")
        await update_project_status(project_id, "error")
        await update_project_deploy_status(project_id, "failed")
        await emit_status_update(project_id, status="error", stage="error", progress={})
        await trace_event(
            project_id,
            "orchestrator",
            "deploy.failed",
            {"error": str(exc)},
            stage="error",
        )
        return {"status": "failed", "project_id": project_id}


async def restart_preview(project_id: str) -> dict[str, Any]:
    project = await get_project(project_id)
    if not project:
        return {"status": "not_found", "project_id": project_id}
    if not project.get("container_id"):
        return {"status": "missing_container", "project_id": project_id}
    if not project.get("port"):
        return {"status": "missing_preview_port", "project_id": project_id}

    validation_result = await validate_workspace_contract_async(
        repository_manager.workspace_path(project_id),
        stack_config=project.get("stack_config", {}),
        container_manager=container_manager,
        container_id=project.get("container_id") or None,
        require_heavy_checks=False,
    )
    if validation_result.blocking_issues:
        return {
            "status": "validation_failed",
            "project_id": project_id,
            "validation": validation_result.to_dict(),
        }
    if validation_result.runtime_spec is None:
        return {"status": "runtime_spec_missing", "project_id": project_id}

    internal_port = int(project.get("stack_config", {}).get("port", 3000) or 3000)
    preview_metadata = await container_manager.start_preview_process(
        project["container_id"],
        internal_port=internal_port,
        external_port=int(project["port"]),
        stack_config=project.get("stack_config", {}),
        workspace_path=repository_manager.workspace_path(project_id),
        restart_reason="manual_restart",
        runtime_spec=validation_result.runtime_spec,
    )
    await container_manager.wait_for_preview(
        project["container_id"],
        internal_port=internal_port,
        probe_path=validation_result.runtime_spec.readiness_path,
    )
    preview_url = project.get("preview_url") or f"http://localhost:{project['port']}"
    metadata = {
        **preview_metadata,
        "internal_port": internal_port,
        "checked_at": _now(),
        "probe_path": validation_result.runtime_spec.readiness_path,
    }
    await update_project_preview(project_id, preview_url, "healthy", metadata)
    await emit_status_update(
        project_id,
        status=project.get("status", "preview"),
        stage=project.get("project_stage", "awaiting_preview_approval"),
        progress={},
        preview_url=preview_url,
    )
    return {"status": "restarted", "project_id": project_id}


async def stop_project(project_id: str) -> dict[str, str]:
    task = _active_runs.pop(project_id, None)
    if task:
        task.cancel()
        await update_project_status(project_id, "paused")
        return {"status": "stopped", "project_id": project_id}
    project = await get_project(project_id)
    if project and project.get("status") in {"planning", "building", "preview"}:
        await update_project_status(project_id, "paused")
        return {"status": "paused", "project_id": project_id}
    return {"status": "not_running", "project_id": project_id}


def is_running(project_id: str) -> bool:
    return project_id in _active_runs


async def get_checkpoints(project_id: str) -> list[dict[str, Any]]:
    graph = await _aget_graph()
    config = _make_thread_config(project_id)

    checkpoints: list[dict[str, Any]] = []
    try:
        async for checkpoint in graph.aget_state_history(config):
            checkpoints.append(
                {
                    "config": checkpoint.config,
                    "created_at": checkpoint.created_at,
                    "parent_config": checkpoint.parent_config,
                    "values_summary": {
                        "project_stage": checkpoint.values.get("project_stage", ""),
                        "status": checkpoint.values.get("status", ""),
                        "current_task_index": checkpoint.values.get("current_task_index", 0),
                        "active_task_id": checkpoint.values.get("active_task_id", ""),
                        "completed_tasks": checkpoint.values.get("completed_tasks", 0),
                        "total_tasks": checkpoint.values.get("total_tasks", 0),
                        "task_attempt_count": checkpoint.values.get("task_attempt_count", 0),
                    },
                }
            )
    except Exception:
        logger.warning("Failed to load checkpoints for %s", project_id)
    return checkpoints


async def get_latest_checkpoint_summary(project_id: str) -> dict[str, Any] | None:
    graph = await _aget_graph()
    config = _make_thread_config(project_id)

    try:
        async for checkpoint in graph.aget_state_history(config):
            return {
                "project_stage": checkpoint.values.get("project_stage", ""),
                "status": checkpoint.values.get("status", ""),
                "current_task_index": checkpoint.values.get("current_task_index", 0),
                "active_task_id": checkpoint.values.get("active_task_id", ""),
                "completed_tasks": checkpoint.values.get("completed_tasks", 0),
                "total_tasks": checkpoint.values.get("total_tasks", 0),
                "task_attempt_count": checkpoint.values.get("task_attempt_count", 0),
                "created_at": checkpoint.created_at,
            }
    except Exception:
        logger.warning("Failed to load latest checkpoint summary for %s", project_id)
    return None


def _should_resume_existing_run(
    project: dict[str, Any],
    checkpoint_summary: dict[str, Any] | None,
) -> bool:
    checkpoint_stage = (checkpoint_summary or {}).get("project_stage") or ""
    if checkpoint_stage and checkpoint_stage not in {"intake", "planning"}:
        return True
    if project.get("status") in {"paused", "error"} and (
        checkpoint_summary is not None or _project_has_meaningful_state(project)
    ):
        return True
    return _project_has_meaningful_state(project) and checkpoint_summary is not None


def _project_has_meaningful_state(project: dict[str, Any]) -> bool:
    return any(
        (
            bool(project.get("plan_md")),
            bool(project.get("execution_contract")),
            bool(project.get("plan_approved")),
            bool(project.get("repo_ready")),
            bool(project.get("container_id")),
            bool(project.get("preview_url")),
            bool(project.get("preview_approved")),
            (project.get("project_stage") or "intake") not in {"intake", "planning"},
        )
    )


def _build_resume_state_patch(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_md": project.get("plan_md", ""),
        "stack_config": project.get("stack_config", {}),
        "llm_config": normalize_llm_config(project.get("llm_config") or default_llm_config()),
        "execution_contract": project.get("execution_contract", {}),
        "contract_version": int(project.get("contract_version", 0) or 0),
        "decision_log": project.get("decision_log", []),
        "plan_delta_history": project.get("plan_delta_history", []),
        "deploy_target": project.get("deploy_target", {}),
        "preview_url": project.get("preview_url") or "",
        "preview_status": project.get("preview_status") or "pending",
        "preview_metadata": project.get("preview_metadata", {}),
        "plan_approved": bool(project.get("plan_approved", False)),
        "preview_approved": bool(project.get("preview_approved", False)),
        "repo_name": project.get("repo_name") or "",
        "repo_url": project.get("repo_url") or "",
        "repo_clone_url": project.get("repo_clone_url") or "",
        "default_branch": project.get("default_branch") or "main",
        "develop_branch": project.get("develop_branch") or "develop",
        "feature_branch": project.get("feature_branch")
        or repository_manager.create_feature_branch_name(project["name"]),
        "repo_ready": bool(project.get("repo_ready", False)),
        "container_id": project.get("container_id") or "",
        "container_port": project.get("port") or 0,
        "active_task_id": project.get("active_task_id") or "",
        "task_attempt_count": int(project.get("task_attempt_count", 0) or 0),
        "task_validation_result": project.get("task_validation_result", {}),
        "last_escalation": project.get("last_escalation", {}),
        "builder_report": project.get("builder_report", {}),
    }
