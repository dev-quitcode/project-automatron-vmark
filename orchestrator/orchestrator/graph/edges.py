"""Conditional edge functions for the Automatron graph."""

from __future__ import annotations

import logging
from typing import Literal

from orchestrator.graph.state import AutomatronState

logger = logging.getLogger(__name__)

MAX_ESCALATIONS = 2
MAX_FAST_RETRIES = 2


def route_after_architect(state: AutomatronState) -> Literal["plan_review", "task_selector"]:
    if state.get("requires_human", False):
        logger.info("Architect produced operator-facing plan -> plan_review")
        return "plan_review"
    logger.info("Architect produced autonomous delta/update -> task_selector")
    return "task_selector"


def route_after_plan_review(state: AutomatronState) -> Literal["repo_prepare", "architect"]:
    if state.get("container_id"):
        logger.info("Plan review approved for existing workspace -> architect")
        return "architect"
    logger.info("Initial plan review approved -> repo_prepare")
    return "repo_prepare"


def route_after_task_selector(state: AutomatronState) -> Literal["builder", "preview_check"]:
    if state["current_task_index"] < 0:
        logger.info("All tasks completed -> preview_check")
        return "preview_check"
    logger.info("Task %d selected -> builder", state["current_task_index"])
    return "builder"


def route_after_validator(
    state: AutomatronState,
) -> Literal["status_classifier", "builder"]:
    """Route after the validator node.

    PASS → status_classifier (proceed to LLM review + workspace validation).
    FAIL with retries remaining → builder (fast deterministic retry).
    FAIL with retries exhausted → status_classifier (let LLM diagnose).
    ERROR → status_classifier (infrastructure issue, let reviewer handle).
    """
    gate_status = state.get("validation_gate_status", "PASS")

    if gate_status == "PASS":
        return "status_classifier"

    if gate_status == "FAIL":
        fast_retry_count = int(state.get("fast_retry_count", 0) or 0)
        if fast_retry_count <= MAX_FAST_RETRIES:
            logger.info(
                "Validation failed for task %s, fast retry %d/%d -> builder",
                state.get("active_task_id", ""),
                fast_retry_count,
                MAX_FAST_RETRIES,
            )
            return "builder"

    # ERROR or exhausted fast retries — let LLM reviewer diagnose
    logger.info(
        "Validation gate %s for task %s -> status_classifier",
        gate_status,
        state.get("active_task_id", ""),
    )
    return "status_classifier"


def route_after_status_classifier(
    state: AutomatronState,
) -> Literal["task_selector", "freeze", "architect", "builder"]:
    status = state["builder_status"]

    if status in ("SUCCESS", "SILENT_DECISION"):
        return "task_selector"

    task_validation_result = state.get("task_validation_result", {})
    if task_validation_result.get("repairable") and not task_validation_result.get("escalate"):
        logger.info("Task %s marked repairable -> builder self-retry", state.get("active_task_id", ""))
        return "builder"

    escalation_count = state.get("escalation_count", 0)
    if escalation_count >= MAX_ESCALATIONS:
        logger.error("Task %d exceeded escalation limit", state["current_task_index"])
        return "freeze"

    return "architect"
