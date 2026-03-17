"""Status classifier node — classifies builder output and persists task results."""

from __future__ import annotations

import json
import logging
import uuid

from langchain_core.messages import HumanMessage, SystemMessage

from orchestrator.api.websocket import emit_builder_log
from orchestrator.docker_engine.manager import ContainerManager
from orchestrator.execution_contract import (
    count_contract_progress,
    mark_contract_task_completed,
    normalize_execution_contract,
    update_contract_task_attempt,
)
from orchestrator.graph.state import AutomatronState
from orchestrator.llm.configuration import default_llm_config, normalize_llm_config
from orchestrator.llm.prompts import load_prompt
from orchestrator.llm.provider import call_llm
from orchestrator.models.project import save_task_log
from orchestrator.observability import trace_event
from orchestrator.plan_parser.parser import mark_task_completed
from orchestrator.repository.manager import RepositoryManager
from orchestrator.validation.workspace import (
    should_run_heavy_task_checks,
    should_validate_release_artifacts,
    validate_workspace_contract_async,
)

logger = logging.getLogger(__name__)

repository_manager = RepositoryManager()
container_manager = ContainerManager()


async def status_classifier_node(state: AutomatronState) -> dict:
    builder_output = state.get("builder_output", "")
    builder_error = state.get("builder_error_detail", "")
    task_text = state.get("current_task_text", "")
    task_index = state.get("current_task_index", -1)
    task_id = state.get("active_task_id", f"task-{task_index + 1:03d}")
    session_id = state.get("session_id", "")
    llm_config = normalize_llm_config(state.get("llm_config") or default_llm_config())
    reviewer_model = llm_config["reviewer"]["model"]
    plan_md = state.get("plan_md", "")
    execution_contract = normalize_execution_contract(state.get("execution_contract") or {})
    current_attempt = int(state.get("task_attempt_count", 0) or 0)
    max_self_retries = int(execution_contract.get("escalation_policy", {}).get("self_retries", 2) or 2)

    await trace_event(
        state["project_id"],
        "reviewer",
        "reviewer.run.started",
        {
            "task_id": task_id,
            "task_index": task_index,
            "current_attempt": current_attempt,
            "builder_exit_code": state.get("builder_exit_code"),
            "builder_status": state.get("builder_status", ""),
        },
        session_id=session_id,
        stage=state.get("project_stage"),
    )

    # Always classify via LLM — naive string heuristics produce false
    # positives/negatives (e.g. "Error handling implemented" triggers failure).
    exit_code = int(state.get("builder_exit_code", -1) or -1)
    system_prompt = load_prompt("reviewer", "v1")
    classification_input = (
        f"Task: {task_text}\n\n"
        f"Builder exit code: {exit_code}\n\n"
        f"Builder Output (last 3000 chars):\n```\n{builder_output[-3000:]}\n```\n\n"
        f"Error Details:\n{builder_error}\n\n"
    )
    # Enrich with validation command failures when the validator exhausted fast retries
    validation_command_results = state.get("validation_command_results") or []
    failed_commands = [r for r in validation_command_results if r.get("exit_code", 0) != 0]
    if failed_commands:
        classification_input += "Validation command failures (from orchestrator):\n" + "\n".join(
            f"- `{r['command']}` (exit {r['exit_code']}): {r['output'][-500:]}"
            for r in failed_commands
        ) + "\n\n"
    classification_input += 'Return JSON: {"status": "...", "reason": "..."}'
    try:
        response = await call_llm(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=classification_input),
            ],
            model=reviewer_model,
            trace_context={
                "project_id": state["project_id"],
                "session_id": session_id,
                "actor": "reviewer",
                "stage": state.get("project_stage"),
                "prompt_name": "reviewer_v1",
            },
        )
        result = _parse_classification(response)
        status = result["status"]
        reason = result.get("reason", "")
    except Exception as exc:
        logger.error("Reviewer classification failed: %s", exc)
        # On classifier failure, use exit code as fallback instead of heuristics
        if exit_code == 0:
            status = "SUCCESS"
            reason = "Classification failed; exit code 0 used as fallback"
        else:
            status = "AMBIGUITY"
            reason = f"Classification failed: {exc}"

    updated_plan = plan_md
    updated_contract = execution_contract
    task_validation_result: dict = {
        "task_id": task_id,
        "passed": False,
        "checks": [],
        "blocking": False,
        "repairable": False,
        "escalate": False,
    }
    next_attempt_count = current_attempt
    last_escalation: dict = {}
    if status in ("SUCCESS", "SILENT_DECISION"):
        # Skip heavy checks when the validator node already confirmed validation_commands pass
        validation_gate_passed = state.get("validation_gate_status") == "PASS"
        require_heavy = not validation_gate_passed and should_run_heavy_task_checks(task_text)
        completed_tasks, total_tasks = count_contract_progress(execution_contract)
        validation_result = await validate_workspace_contract_async(
            repository_manager.workspace_path(state["project_id"]),
            stack_config=state.get("stack_config", {}),
            container_manager=container_manager,
            container_id=state.get("container_id") or None,
            require_heavy_checks=require_heavy,
            include_release_artifacts=should_validate_release_artifacts(
                task_text,
                completed_tasks=completed_tasks,
                total_tasks=total_tasks,
            ),
        )
        blocking_issues = validation_result.blocking_issues
        if blocking_issues:
            task_validation_result = {
                "task_id": task_id,
                "passed": False,
                "checks": [
                    {
                        "code": issue.code,
                        "status": issue.status,
                        "message": issue.message,
                        "details": issue.details,
                    }
                    for issue in validation_result.issues
                ],
                "blocking": True,
                "repairable": _issues_repairable(blocking_issues),
                "escalate": False,
            }
            status = (
                "BLOCKER"
                if any(issue.status == "blocker" for issue in blocking_issues)
                else "AMBIGUITY"
            )
            reason = "; ".join(issue.message for issue in blocking_issues)
            next_attempt_count = current_attempt + 1
            if task_validation_result["repairable"] and next_attempt_count <= max_self_retries:
                updated_contract = update_contract_task_attempt(
                    execution_contract,
                    task_id,
                    next_attempt_count,
                    status="retrying",
                )
            else:
                task_validation_result["escalate"] = True
                last_escalation = _build_escalation_request(
                    state,
                    task_id=task_id,
                    task_text=task_text,
                    reason=reason,
                    validation_result=task_validation_result,
                    attempts_made=next_attempt_count,
                )
                updated_contract = update_contract_task_attempt(
                    execution_contract,
                    task_id,
                    next_attempt_count,
                    status="blocked",
                )
        else:
            task_validation_result = {
                "task_id": task_id,
                "passed": True,
                "checks": [],
                "blocking": False,
                "repairable": False,
                "escalate": False,
            }
            updated_contract = mark_contract_task_completed(execution_contract, task_id)

    if status in ("SUCCESS", "SILENT_DECISION"):
        if plan_md:
            try:
                updated_plan = mark_task_completed(plan_md, task_index)
                workspace_plan = repository_manager.workspace_path(state["project_id"]) / "PLAN.md"
                workspace_plan.write_text(updated_plan, encoding="utf-8")
            except Exception as exc:
                logger.warning("Failed to mark task %d complete in PLAN.md: %s", task_index, exc)
        _update_stories_status(
            workspace=repository_manager.workspace_path(state["project_id"]),
            task_id=task_id,
        )
        commit_message = f"builder: task {task_index + 1} {task_text.splitlines()[0][:72]}"
        try:
            repository_manager.commit_workspace_changes(
                state["project_id"],
                commit_message,
                branch=state.get("feature_branch") or None,
            )
        except Exception as exc:
            logger.warning("Failed to commit task %d changes: %s", task_index, exc)

    _append_learning(
        workspace=repository_manager.workspace_path(state["project_id"]),
        task_id=task_id,
        task_text=task_text,
        status=status,
        attempt_count=current_attempt,
        reason=reason,
        duration_s=float(state.get("builder_duration_s", 0) or 0),
    )

    # On escalation: if the architect updated the architecture doc, write it to disk.
    updated_architecture_md = state.get("architecture_md", "")
    if updated_architecture_md and task_validation_result.get("escalate"):
        try:
            arch_path = repository_manager.workspace_path(state["project_id"]) / "docs" / "ARCHITECTURE.md"
            if arch_path.exists():
                arch_path.write_text(updated_architecture_md.strip() + "\n", encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to update ARCHITECTURE.md: %s", exc)
    else:
        next_attempt_count = current_attempt + 1
        repairable_failure = _reason_is_repairable(reason, builder_output, builder_error)
        task_validation_result = {
            **task_validation_result,
            "task_id": task_id,
            "passed": False,
            "checks": task_validation_result.get("checks", []),
            "blocking": True,
            "repairable": repairable_failure,
            "escalate": not (repairable_failure and next_attempt_count <= max_self_retries),
        }
        updated_contract = update_contract_task_attempt(
            updated_contract,
            task_id,
            next_attempt_count,
            status="retrying" if not task_validation_result["escalate"] else "failed",
        )
        if task_validation_result["escalate"] and not last_escalation:
            last_escalation = _build_escalation_request(
                state,
                task_id=task_id,
                task_text=task_text,
                reason=reason,
                validation_result=task_validation_result,
                attempts_made=next_attempt_count,
            )

    if session_id:
        await save_task_log(
            str(uuid.uuid4()),
            session_id,
            task_index,
            task_text,
            status,
            builder_output,
            float(state.get("builder_duration_s", 0.0)),
        )

    await emit_builder_log(
        state["project_id"],
        task_index=task_index,
        task_text=task_text,
        output=builder_output,
        status=status,
    )

    builder_report = dict(state.get("builder_report") or {})
    builder_report.update(
        {
            "task_id": task_id,
            "status": status.lower(),
            "validation_summary": task_validation_result,
            "issues": task_validation_result.get("checks", []),
            "needs_escalation": task_validation_result.get("escalate", False),
        }
    )

    needs_self_retry = bool(task_validation_result.get("repairable")) and not task_validation_result.get("escalate")
    next_stage = (
        "awaiting_architect_delta"
        if task_validation_result.get("escalate")
        else ("building" if needs_self_retry else "validating")
    )
    next_status = "building" if task_validation_result.get("escalate") or needs_self_retry else "validating"

    await trace_event(
        state["project_id"],
        "reviewer",
        "reviewer.run.completed",
        {
            "task_id": task_id,
            "status": status,
            "reason": reason,
            "repairable": task_validation_result.get("repairable", False),
            "escalate": task_validation_result.get("escalate", False),
            "next_stage": next_stage,
            "next_status": next_status,
            "attempt_count": 0 if status in ("SUCCESS", "SILENT_DECISION") else (next_attempt_count or current_attempt + 1),
        },
        session_id=session_id,
        stage=next_stage,
    )

    return {
        "builder_status": status,
        "builder_error_detail": reason,
        "plan_md": updated_plan,
        "execution_contract": updated_contract,
        "task_validation_result": task_validation_result,
        "task_attempt_count": 0 if status in ("SUCCESS", "SILENT_DECISION") else (next_attempt_count or current_attempt + 1),
        "last_escalation": last_escalation,
        "builder_report": builder_report,
        "project_stage": next_stage,
        "status": next_status,
    }


def _looks_successful(output: str) -> bool:
    """Kept for backward compatibility but no longer used in the main classification path.

    The main flow now always delegates to the LLM classifier + deterministic
    validators because naive string matching produces false positives
    (e.g. "Error handling implemented successfully" would wrongly fail).
    """
    output_lower = output.lower()
    error_indicators = [
        "error:",
        "error!",
        "failed",
        "exception",
        "traceback",
        "fatal",
        "cannot find",
        "not found",
        "permission denied",
        "enoent",
        "eacces",
    ]
    return not any(indicator in output_lower for indicator in error_indicators)


def _issues_repairable(issues: list) -> bool:
    markers = (
        "build",
        "compile",
        "import",
        "prisma",
        "artifact",
        "health",
        "metadata",
        "workflow",
        "compose",
    )
    return all(any(marker in issue.code for marker in markers) for issue in issues)


def _reason_is_repairable(reason: str, builder_output: str, builder_error: str) -> bool:
    haystack = f"{reason}\n{builder_output}\n{builder_error}".lower()
    markers = (
        "build",
        "compile",
        "import",
        "type error",
        "lint",
        "prisma",
        "artifact",
        "health",
        "metadata",
        "workflow",
        "compose",
        "module not found",
        "cannot find module",
    )
    return any(marker in haystack for marker in markers)


def _build_escalation_request(
    state: AutomatronState,
    *,
    task_id: str,
    task_text: str,
    reason: str,
    validation_result: dict,
    attempts_made: int,
) -> dict:
    builder_report = state.get("builder_report") or {}
    return {
        "task_id": task_id,
        "problem_type": state.get("builder_status") or "BLOCKER",
        "observed_error": reason,
        "commands_run": list(builder_report.get("commands_run", [])),
        "files_touched": list(builder_report.get("files_touched", [])),
        "attempts_made": attempts_made,
        "task_text": task_text,
        "validation_result": validation_result,
        "recommended_option": "Return a targeted architect plan delta for this task.",
    }

def _append_learning(
    *,
    workspace: "Path",
    task_id: str,
    task_text: str,
    status: str,
    attempt_count: int,
    reason: str,
    duration_s: float,
) -> None:
    """Append a learning entry to docs/LEARNINGS.md. Never raises — failures are logged."""
    from datetime import datetime, timezone
    from pathlib import Path

    learnings_path = Path(workspace) / "docs" / "LEARNINGS.md"
    if not learnings_path.parent.exists():
        return
    try:
        first_line = task_text.splitlines()[0][:80] if task_text else task_id
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = (
            f"\n## {task_id}: {first_line} ({timestamp})\n"
            f"- **Status:** {status}\n"
            f"- **Attempt:** {attempt_count + 1}\n"
            f"- **Duration:** {duration_s:.0f}s\n"
            f"- **Notes:** {reason[:300] if reason else 'n/a'}\n"
        )
        existing = learnings_path.read_text(encoding="utf-8") if learnings_path.exists() else "# Learnings\n"
        learnings_path.write_text(existing.rstrip() + "\n" + entry, encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to append to LEARNINGS.md: %s", exc)


def _update_stories_status(*, workspace: "Path", task_id: str) -> None:
    """Mark a task as completed in docs/STORIES.md using checkbox replacement. Never raises."""
    from pathlib import Path

    stories_path = Path(workspace) / "docs" / "STORIES.md"
    if not stories_path.exists():
        return
    try:
        content = stories_path.read_text(encoding="utf-8")
        # Match lines like: - [ ] `task-001` ...  or  - [ ] `task-001-abc123` ...
        updated = content.replace(f"- [ ] `{task_id}`", f"- [x] `{task_id}`")
        if updated != content:
            stories_path.write_text(updated, encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to update STORIES.md for %s: %s", task_id, exc)


def _parse_classification(response: str) -> dict[str, str]:
    try:
        if "```json" in response:
            start = response.index("```json") + len("```json")
            end = response.index("```", start)
            return json.loads(response[start:end].strip())
        if "```" in response:
            start = response.index("```") + 3
            end = response.index("```", start)
            return json.loads(response[start:end].strip())
        return json.loads(response.strip())
    except (ValueError, json.JSONDecodeError):
        pass

    response_upper = response.upper()
    for status in ("BLOCKER", "AMBIGUITY", "SILENT_DECISION", "SUCCESS"):
        if status in response_upper:
            return {"status": status, "reason": response[:200]}

    return {"status": "AMBIGUITY", "reason": "Could not parse classification"}
