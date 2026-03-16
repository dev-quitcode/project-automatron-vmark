"""Scaffold, approval, and preview nodes for the Automatron graph."""

from __future__ import annotations

import logging
import shlex
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt

from orchestrator.config import settings
from orchestrator.docker_engine.manager import ContainerManager
from orchestrator.docker_engine.port_allocator import PortAllocator
from orchestrator.execution_contract import count_contract_progress, get_next_contract_task
from orchestrator.graph.state import AutomatronState
from orchestrator.llm.configuration import (
    builder_auth_provider,
    default_llm_config,
    normalize_llm_config,
    provider_api_key,
)
from orchestrator.plan_parser.parser import get_next_task, get_progress
from orchestrator.repository.manager import RepositoryManager
from orchestrator.observability import trace_event
from orchestrator.validation.workspace import validate_workspace_contract_async

logger = logging.getLogger(__name__)

container_manager = ContainerManager()
port_allocator = PortAllocator(start=settings.port_range_start, end=settings.port_range_end)
repository_manager = RepositoryManager()

INIT_SCRIPT_ALIASES = {
    "init-framework.sh": "init-generic.sh",
    "init-nextjs-app-router.sh": "init-nextjs.sh",
    "init-nextjs-app.sh": "init-nextjs.sh",
    "init-nextjs-typescript.sh": "init-nextjs.sh",
    "init-vite-react.sh": "init-react-vite.sh",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_init_script(stack_config: dict) -> str:
    configured = str(stack_config.get("init_script", "") or "").strip()
    configured = INIT_SCRIPT_ALIASES.get(configured, configured)
    known_scripts = set(INIT_SCRIPT_ALIASES.values()) | {
        "init-nextjs.sh",
        "init-react-vite.sh",
        "init-python.sh",
        "init-generic.sh",
    }
    if configured in known_scripts:
        return configured

    stack_text = " ".join(
        str(stack_config.get(key, "") or "")
        for key in ("stack", "framework", "package_manager", "db", "orm", "styling")
    ).lower()

    if "next" in stack_text:
        return "init-nextjs.sh"
    if "vite" in stack_text or "react" in stack_text:
        return "init-react-vite.sh"
    if "python" in stack_text or "fastapi" in stack_text or "django" in stack_text or "flask" in stack_text:
        return "init-python.sh"
    return "init-generic.sh"


def _bootstrap_marker_command(init_script: str) -> str | None:
    if init_script == "init-nextjs.sh":
        return "test -f /workspace/package.json && test -f /workspace/app/layout.tsx"
    if init_script == "init-react-vite.sh":
        return "test -f /workspace/package.json && test -d /workspace/src"
    if init_script == "init-python.sh":
        return "test -f /workspace/pyproject.toml || test -f /workspace/requirements.txt"
    return None


async def plan_review_node(state: AutomatronState) -> dict:
    """Pause after plan generation or freeze escalation."""
    if state.get("container_id") and not state.get("requires_human", False):
        return {
            "requires_human": False,
            "human_intervention_reason": "",
            "project_stage": "building",
            "status": "building",
        }

    project_id = state["project_id"]
    reason = state.get("human_intervention_reason") or "Review and approve the technical plan."
    approval = interrupt(
        {
            "type": "plan_review",
            "project_id": project_id,
            "plan_md": state.get("plan_md", ""),
            "reason": reason,
        }
    )

    feedback = _extract_feedback(approval)
    result: dict = {
        "requires_human": False,
        "human_intervention_reason": "",
        "status": "planning",
    }

    if state.get("container_id"):
        result["project_stage"] = "planning"
    else:
        result["plan_approved"] = True
        result["plan_approved_at"] = _now()
        result["project_stage"] = "repo_preparing"

    if feedback:
        result["messages"] = [HumanMessage(content=f"[Human Feedback] {feedback}")]

    return result


async def repo_prepare_node(state: AutomatronState) -> dict:
    """Create the remote repository and reserve branch names."""
    metadata = await repository_manager.create_remote_repository(
        state["project_id"],
        state["project_name"],
    )
    return {
        "repo_name": metadata.repo_name,
        "repo_url": metadata.repo_url,
        "repo_clone_url": metadata.repo_clone_url,
        "default_branch": metadata.default_branch,
        "develop_branch": metadata.develop_branch,
        "feature_branch": metadata.feature_branch,
        "project_stage": "repo_preparing",
        "status": "planning",
    }


async def scaffold_node(state: AutomatronState) -> dict:
    """Create the sandbox container and bootstrap the local git workspace."""
    project_id = state["project_id"]
    stack_config = state.get("stack_config", {})
    port = await port_allocator.allocate(project_id)

    container_info = await container_manager.create_project_container(
        project_id=project_id,
        stack_config=stack_config,
        port=port,
    )

    init_script = _resolve_init_script(stack_config)
    init_timeout = 900 if init_script == "init-nextjs.sh" else 300
    llm_config = normalize_llm_config(state.get("llm_config") or default_llm_config())
    builder_provider = llm_config["builder"]["provider"]
    builder_model = llm_config["builder"]["model"]
    script_path = f"/opt/automatron/scripts/{init_script}"
    await trace_event(
        project_id,
        "orchestrator",
        "scaffold.bootstrap.started",
        {
            "init_script": init_script,
            "timeout_seconds": init_timeout,
            "stack": stack_config.get("stack", ""),
            "framework": stack_config.get("framework", ""),
        },
        session_id=state.get("session_id"),
        stage="scaffolding",
    )
    try:
        init_result = await container_manager.exec_in_container(
            container_info.container_id,
            f"bash {script_path}",
            timeout=init_timeout,
        )
        if init_result.exit_code != 0:
            raise RuntimeError(
                f"Bootstrap script {init_script} failed with exit code {init_result.exit_code}: {init_result.output[-4000:]}"
            )
        marker_command = _bootstrap_marker_command(init_script)
        if marker_command:
            marker_result = await container_manager.exec_in_container(
                container_info.container_id,
                marker_command,
                timeout=15,
            )
            if marker_result.exit_code != 0:
                raise RuntimeError(
                    f"Bootstrap script {init_script} completed without expected workspace markers"
                )
        await trace_event(
            project_id,
            "orchestrator",
            "scaffold.bootstrap.completed",
            {"init_script": init_script},
            session_id=state.get("session_id"),
            stage="scaffolding",
        )
    except Exception as exc:
        await trace_event(
            project_id,
            "orchestrator",
            "scaffold.bootstrap.failed",
            {"init_script": init_script, "error": str(exc)},
            session_id=state.get("session_id"),
            stage="error",
        )
        logger.warning("Init script %s failed: %s", init_script, exc)
        raise

    provider_key = provider_api_key(builder_provider)
    if provider_key:
        try:
            await container_manager.exec_in_container(
                container_info.container_id,
                " ".join(
                    [
                        "cline auth",
                        f"-p {builder_auth_provider(builder_provider)}",
                        f"-k {shlex.quote(provider_key)}",
                        f"-m {shlex.quote(builder_model)}",
                    ]
                ),
                timeout=30,
            )
        except Exception as exc:
            logger.warning("Cline auth setup failed: %s", exc)

    repository_manager.ensure_deploy_supporting_docs(project_id, state["project_name"])
    repository_manager.initialize_workspace_repository(
        project_id,
        state["project_name"],
        repository_manager_metadata_from_state(state),
    )
    await repository_manager.ensure_remote_cicd(repository_manager_metadata_from_state(state))

    return {
        "container_id": container_info.container_id,
        "container_port": port,
        "repo_ready": True,
        "project_stage": "building",
        "status": "building",
    }


async def task_selector_node(state: AutomatronState) -> dict:
    plan_md = state.get("plan_md", "")
    execution_contract = state.get("execution_contract") or {}

    if execution_contract:
        completed_tasks, total_tasks = count_contract_progress(execution_contract)
        next_task_contract = get_next_contract_task(execution_contract)
        if next_task_contract is None:
            return {
                "active_task_id": "",
                "current_task_index": -1,
                "current_task_text": "",
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "escalation_count": 0,
                "task_attempt_count": 0,
                "task_validation_result": {},
                "fast_retry_count": 0,
                "validation_gate_status": "",
                "validation_command_results": [],
                "project_stage": "building",
                "status": "building",
            }

        previous_task_id = state.get("active_task_id", "")
        same_task = previous_task_id == next_task_contract.get("task_id", "")
        return {
            "active_task_id": next_task_contract.get("task_id", ""),
            "current_task_index": _extract_task_index(next_task_contract.get("task_id", "task-001")),
            "current_task_text": _task_contract_to_text(next_task_contract),
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "escalation_count": state.get("escalation_count", 0) if same_task else 0,
            "task_attempt_count": state.get("task_attempt_count", 0) if same_task else 0,
            "fast_retry_count": state.get("fast_retry_count", 0) if same_task else 0,
            "validation_gate_status": "" if not same_task else state.get("validation_gate_status", ""),
            "validation_command_results": [] if not same_task else state.get("validation_command_results", []),
            "builder_status": "",
            "builder_output": "",
            "builder_error_detail": "",
            "project_stage": "building",
            "status": "building",
        }

    progress = get_progress(plan_md)
    next_task = get_next_task(plan_md)

    if next_task is None:
        return {
            "active_task_id": "",
            "current_task_index": -1,
            "current_task_text": "",
            "total_tasks": progress.total,
            "completed_tasks": progress.completed,
            "escalation_count": 0,
            "task_attempt_count": 0,
            "task_validation_result": {},
            "fast_retry_count": 0,
            "validation_gate_status": "",
            "validation_command_results": [],
            "project_stage": "building",
            "status": "building",
        }

    previous_index = state.get("current_task_index", -1)
    escalation_count = 0 if next_task.index != previous_index else state.get("escalation_count", 0)

    new_task = next_task.index != previous_index
    return {
        "active_task_id": f"task-{next_task.index + 1:03d}",
        "current_task_index": next_task.index,
        "current_task_text": f"{next_task.title}\n{next_task.description}".strip(),
        "total_tasks": progress.total,
        "completed_tasks": progress.completed,
        "escalation_count": escalation_count,
        "task_attempt_count": 0 if new_task else state.get("task_attempt_count", 0),
        "fast_retry_count": 0 if new_task else state.get("fast_retry_count", 0),
        "validation_gate_status": "" if new_task else state.get("validation_gate_status", ""),
        "validation_command_results": [] if new_task else state.get("validation_command_results", []),
        "builder_status": "",
        "builder_output": "",
        "builder_error_detail": "",
        "project_stage": "building",
        "status": "building",
    }


async def freeze_node(state: AutomatronState) -> dict:
    task_index = state.get("current_task_index", -1)
    task_text = state.get("current_task_text", "")
    escalation_count = state.get("escalation_count", 0)
    reason = (
        f"Task #{task_index + 1} failed {escalation_count + 1} times.\n"
        f"Task: {task_text[:200]}\n"
        f"Last error: {state.get('builder_error_detail', '')[:500]}"
    )

    history = list(state.get("escalation_history", []))
    history.append(
        {
            "task_index": task_index,
            "status": "FROZEN",
            "timestamp": _now(),
            "reason": reason,
        }
    )
    return {
        "project_stage": "frozen",
        "status": "frozen",
        "requires_human": True,
        "human_intervention_reason": reason,
        "escalation_history": history,
    }


async def preview_check_node(state: AutomatronState) -> dict:
    project_id = state["project_id"]
    validation_result = await validate_workspace_contract_async(
        repository_manager.workspace_path(project_id),
        stack_config=state.get("stack_config", {}),
        container_manager=container_manager,
        container_id=state.get("container_id") or None,
        require_heavy_checks=True,
        include_release_artifacts=True,
    )
    if validation_result.blocking_issues:
        # Return error state instead of crashing the graph — allows the
        # operator to inspect, restart preview, or resume from this point.
        blocking_summary = "; ".join(
            issue.message for issue in validation_result.blocking_issues
        )
        logger.error("Preview check blocked for %s: %s", project_id, blocking_summary)
        return {
            "preview_url": "",
            "preview_status": "failed",
            "preview_metadata": {
                "error": blocking_summary,
                "checked_at": _now(),
            },
            "project_stage": "frozen",
            "status": "frozen",
            "requires_human": True,
            "human_intervention_reason": (
                f"Preview validation failed: {blocking_summary}\n"
                "Fix the issues and restart the preview, or approve to skip."
            ),
        }
    if validation_result.runtime_spec is None:
        logger.error("Could not resolve preview runtime spec for %s", project_id)
        return {
            "preview_url": "",
            "preview_status": "failed",
            "preview_metadata": {
                "error": "Could not resolve preview runtime spec",
                "checked_at": _now(),
            },
            "project_stage": "frozen",
            "status": "frozen",
            "requires_human": True,
            "human_intervention_reason": "Could not resolve preview runtime spec. Check workspace structure.",
        }

    repository_manager.commit_workspace_changes(
        project_id,
        "chore: finalize preview-ready workspace",
        branch=state.get("feature_branch") or None,
    )

    internal_port = int(state.get("stack_config", {}).get("port", 3000) or 3000)
    preview_metadata = await container_manager.start_preview_process(
        state["container_id"],
        internal_port=internal_port,
        external_port=state["container_port"],
        stack_config=state.get("stack_config", {}),
        workspace_path=repository_manager.workspace_path(project_id),
        restart_reason="preview_check",
        runtime_spec=validation_result.runtime_spec,
    )
    await container_manager.wait_for_preview(
        state["container_id"],
        internal_port=internal_port,
        probe_path=validation_result.runtime_spec.readiness_path,
    )

    preview_url = f"http://localhost:{state['container_port']}"
    return {
        "preview_url": preview_url,
        "preview_status": "healthy",
        "preview_metadata": {
            **preview_metadata,
            "internal_port": internal_port,
            "checked_at": _now(),
            "probe_path": validation_result.runtime_spec.readiness_path,
        },
        "project_stage": "awaiting_preview_approval",
        "status": "preview",
        "requires_human": True,
        "human_intervention_reason": "Review the live preview before promotion to develop.",
    }


async def preview_review_node(state: AutomatronState) -> dict:
    approval = interrupt(
        {
            "type": "preview_review",
            "project_id": state["project_id"],
            "preview_url": state.get("preview_url"),
            "reason": state.get("human_intervention_reason", "Review preview"),
        }
    )
    feedback = _extract_feedback(approval)
    result: dict = {
        "preview_approved": True,
        "preview_approved_at": _now(),
        "requires_human": False,
        "human_intervention_reason": "",
        "project_stage": "ready_for_deploy",
        "status": "ready_for_deploy",
    }
    if feedback:
        result["messages"] = [HumanMessage(content=f"[Preview Feedback] {feedback}")]
    return result


async def ready_for_deploy_node(state: AutomatronState) -> dict:
    repository_manager.merge_branch(
        state["project_id"],
        state["feature_branch"],
        state["develop_branch"],
        "chore: promote approved preview to develop",
    )
    return {
        "project_stage": "ready_for_deploy",
        "status": "ready_for_deploy",
        "requires_human": False,
    }


def _extract_feedback(value: object) -> str | None:
    if isinstance(value, dict):
        feedback = value.get("feedback")
        return str(feedback).strip() if feedback else None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def repository_manager_metadata_from_state(state: AutomatronState):
    from orchestrator.repository.manager import RepoMetadata

    return RepoMetadata(
        repo_name=state["repo_name"],
        repo_url=state["repo_url"],
        repo_clone_url=state["repo_clone_url"],
        default_branch=state.get("default_branch", "main"),
        develop_branch=state.get("develop_branch", "develop"),
        feature_branch=state.get("feature_branch", "feature/1-project"),
    )


def _extract_task_index(task_id: str) -> int:
    """Extract the numeric index from a task ID like 'task-001' or 'task-001-abc123'."""
    parts = task_id.split("-")
    if len(parts) >= 2:
        try:
            return int(parts[1]) - 1
        except ValueError:
            pass
    return 0


def _task_contract_to_text(task_contract: dict) -> str:
    lines = [str(task_contract.get("title", "Task")).strip()]
    goal = str(task_contract.get("goal", "")).strip()
    if goal:
        lines.append(goal)
    for done_when in task_contract.get("done_when", []):
        lines.append(f"Done when: {done_when}")
    for command in task_contract.get("validation_commands", []):
        lines.append(f"Validate with: {command}")
    return "\n".join(line for line in lines if line)
