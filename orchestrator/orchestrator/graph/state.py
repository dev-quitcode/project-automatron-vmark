"""LangGraph state schema for the Automatron orchestrator."""

from __future__ import annotations

from typing import Annotated, Literal

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

ProjectStage = Literal[
    "intake",
    "planning",
    "awaiting_plan_approval",
    "repo_preparing",
    "scaffolding",
    "building",
    "validating",
    "awaiting_architect_delta",
    "awaiting_preview_approval",
    "ready_for_deploy",
    "deploying",
    "deployed",
    "frozen",
    "error",
]

ProjectStatus = Literal[
    "pending",
    "planning",
    "building",
    "validating",
    "preview",
    "ready_for_deploy",
    "deploying",
    "deployed",
    "paused",
    "frozen",
    "error",
]

BuilderStatus = Literal["SUCCESS", "BLOCKER", "AMBIGUITY", "SILENT_DECISION", ""]


class DeployTarget(TypedDict, total=False):
    auth_mode: str
    host: str
    port: int
    user: str
    deploy_path: str
    auth_reference: str
    ssh_private_key: str
    ssh_password: str
    known_hosts: str
    env_content: str
    app_url: str
    health_path: str


class LlmRoleConfig(TypedDict, total=False):
    provider: str
    model: str


class AutomatronState(TypedDict, total=False):
    project_id: str
    project_name: str
    intake_text: str
    intake_source: str
    source_ref: str
    session_id: str

    plan_md: str
    stack_config: dict
    llm_config: dict[str, LlmRoleConfig]
    execution_contract: dict
    contract_version: int
    decision_log: list[dict]
    plan_delta_history: list[dict]

    current_task_index: int
    active_task_id: str
    current_task_text: str
    total_tasks: int
    completed_tasks: int
    task_attempt_count: int
    task_validation_result: dict
    last_escalation: dict
    builder_report: dict

    messages: Annotated[list[AnyMessage], add_messages]

    builder_status: BuilderStatus
    builder_output: str
    builder_error_detail: str
    builder_exit_code: int
    builder_duration_s: float
    architect_prompt_version: str

    escalation_count: int
    escalation_history: list[dict]

    container_id: str
    container_port: int

    repo_name: str
    repo_url: str
    repo_clone_url: str
    default_branch: str
    develop_branch: str
    feature_branch: str
    repo_ready: bool

    fast_retry_count: int
    validation_gate_status: str
    validation_command_results: list

    preview_url: str
    preview_status: str
    preview_metadata: dict

    deploy_target: DeployTarget

    project_stage: ProjectStage
    status: ProjectStatus

    requires_human: bool
    human_intervention_reason: str
    plan_approved: bool
    preview_approved: bool
    plan_approved_at: str
    preview_approved_at: str
