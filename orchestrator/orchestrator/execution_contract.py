"""Machine-readable execution contract for Architect/Builder/Reviewer coordination."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from orchestrator.plan_parser.parser import get_global_rules, parse_plan
from orchestrator.validation.runtime import resolve_preview_runtime_spec

DEFAULT_ALLOWED_AUTONOMY = [
    "fix_compile_errors",
    "fix_type_errors",
    "fix_import_paths",
    "fix_lint_errors",
    "adapt_generated_scaffold",
    "create_missing_required_artifacts",
]

DEFAULT_ESCALATE_IF = [
    "task_contract_contradiction",
    "missing_design_decision",
    "requires_api_redesign",
    "requires_schema_redesign",
    "stack_change_required",
]


def extract_json_blocks(response: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for match in re.finditer(r"```json\s*(.*?)```", response, flags=re.DOTALL):
        try:
            payload = json.loads(match.group(1).strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            blocks.append(payload)
    return blocks


def extract_execution_contract(response: str) -> dict[str, Any] | None:
    for payload in extract_json_blocks(response):
        if isinstance(payload.get("task_graph"), list):
            return payload
    return None


def extract_plan_delta(response: str) -> dict[str, Any] | None:
    for payload in extract_json_blocks(response):
        if payload.get("type") == "plan_delta" or "changed_task_ids" in payload:
            return payload
    return None


def _stable_task_id(index: int, title: str) -> str:
    """Generate a stable task ID from a short hash of the title.

    This prevents ID drift when the architect reorders or inserts tasks
    during re-planning.  The index prefix keeps IDs roughly sortable.
    """
    digest = hashlib.sha256(title.encode()).hexdigest()[:6]
    return f"task-{index + 1:03d}-{digest}"


def build_execution_contract(
    *,
    project_name: str,
    intake_text: str,
    plan_md: str,
    stack_config: dict[str, Any] | None,
    existing_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parsed = parse_plan(plan_md)
    runtime_spec = resolve_preview_runtime_spec(
        workspace_path=_VirtualWorkspace.from_stack(stack_config or {}),
        stack_config=stack_config or {},
    )
    # Build lookup maps for existing tasks by both ID and title so we can
    # preserve state (attempt_count, autonomy overrides) across re-plans
    # even when the architect reorders tasks.
    existing_by_id: dict[str, dict[str, Any]] = {}
    existing_by_title: dict[str, dict[str, Any]] = {}
    for task in (existing_contract or {}).get("task_graph", []):
        if isinstance(task, dict):
            tid = task.get("task_id", "")
            existing_by_id[tid] = task
            title = task.get("title", "")
            if title:
                existing_by_title[title] = task
    task_graph: list[dict[str, Any]] = []

    for index, task in enumerate(parsed.tasks):
        task_id = _stable_task_id(index, task.title)
        # Try matching by title first (stable across reordering), then by
        # positional ID for backward compatibility with older contracts.
        positional_id = f"task-{index + 1:03d}"
        previous = (
            existing_by_title.get(task.title)
            or existing_by_id.get(task_id)
            or existing_by_id.get(positional_id)
            or {}
        )
        prev_dep_id = _stable_task_id(index - 1, parsed.tasks[index - 1].title) if index > 0 else None
        # Prefer architect-provided validation_commands when they exist;
        # fall back to auto-generated defaults otherwise.
        prev_commands = previous.get("validation_commands", [])
        validation_commands = (
            prev_commands
            if prev_commands
            else _default_validation_commands(runtime_spec, stack_config or {}, task.description)
        )
        task_graph.append(
            {
                "task_id": task_id,
                "title": task.title,
                "goal": task.description.splitlines()[0] if task.description else task.title,
                "scope": {
                    "phase": task.phase,
                    "description": task.description,
                },
                "inputs": {
                    "phase": task.phase,
                    "plan_line": task.line_number,
                },
                "files_expected": previous.get("files_expected", []),
                "done_when": _build_done_when(task.description),
                "validation_commands": validation_commands,
                "allowed_autonomy": previous.get("allowed_autonomy", DEFAULT_ALLOWED_AUTONOMY),
                "escalate_if": previous.get("escalate_if", DEFAULT_ESCALATE_IF),
                "depends_on": [prev_dep_id] if prev_dep_id else [],
                "epic": previous.get("epic", ""),
                "story_id": previous.get("story_id", ""),
                "story": previous.get("story", ""),
                "status": "completed" if _is_task_completed(plan_md, task.line_number) else "pending",
                "completed": _is_task_completed(plan_md, task.line_number),
                "attempt_count": int(previous.get("attempt_count", 0)),
            }
        )

    decision_log = list((existing_contract or {}).get("decision_log", []))
    if not decision_log:
        decision_log = [
            {
                "id": "decision-001",
                "type": "stack_contract",
                "summary": f"Use {stack_config.get('framework', 'the selected stack')} for the generated MVP.",
            }
        ]

    contract_version = int((existing_contract or {}).get("contract_version", 0)) + 1
    return {
        "project_meta": {
            "name": project_name,
            "intake_text": intake_text,
        },
        "stack_contract": stack_config or {},
        "global_rules": get_global_rules(plan_md),
        "decision_log": decision_log,
        "task_graph": task_graph,
        "validation_contract": {
            "health_path": "/api/health",
            "required_artifacts": [
                "Dockerfile",
                ".env.example",
                "deploy/docker-compose.yml",
                "DEPLOY.md",
                ".github/workflows/ci.yml",
                ".github/workflows/deploy.yml",
            ],
            "preview_runtime": {
                "stack": runtime_spec.stack,
                "package_manager": runtime_spec.package_manager,
                "preview_command_template": runtime_spec.preview_command_template,
                "readiness_path": runtime_spec.readiness_path,
            },
        },
        "escalation_policy": {
            "self_retries": 2,
            "freeze_after_architect_cycles": 2,
        },
        "contract_version": contract_version,
    }


def normalize_execution_contract(contract: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(contract, dict):
        return {}
    normalized = copy.deepcopy(contract)
    normalized.setdefault("project_meta", {})
    normalized.setdefault("stack_contract", {})
    normalized.setdefault("global_rules", [])
    normalized.setdefault("decision_log", [])
    normalized.setdefault("task_graph", [])
    normalized.setdefault("validation_contract", {})
    normalized.setdefault("escalation_policy", {"self_retries": 2, "freeze_after_architect_cycles": 2})
    normalized["contract_version"] = int(normalized.get("contract_version", 1) or 1)
    for index, task in enumerate(normalized["task_graph"]):
        task.setdefault("task_id", f"task-{index + 1:03d}")
        task.setdefault("done_when", [])
        task.setdefault("validation_commands", [])
        task.setdefault("allowed_autonomy", list(DEFAULT_ALLOWED_AUTONOMY))
        task.setdefault("escalate_if", list(DEFAULT_ESCALATE_IF))
        task.setdefault("depends_on", [])
        task.setdefault("epic", "")
        task.setdefault("story_id", "")
        task.setdefault("story", "")
        task["completed"] = bool(task.get("completed", False))
        task["attempt_count"] = int(task.get("attempt_count", 0) or 0)
        task.setdefault("status", "completed" if task["completed"] else "pending")
    return normalized


def get_next_contract_task(execution_contract: dict[str, Any]) -> dict[str, Any] | None:
    contract = normalize_execution_contract(execution_contract)
    for task in contract.get("task_graph", []):
        if not task.get("completed"):
            return task
    return None


def count_contract_progress(execution_contract: dict[str, Any]) -> tuple[int, int]:
    contract = normalize_execution_contract(execution_contract)
    tasks = contract.get("task_graph", [])
    completed = sum(1 for task in tasks if task.get("completed"))
    return completed, len(tasks)


def mark_contract_task_completed(execution_contract: dict[str, Any], task_id: str) -> dict[str, Any]:
    contract = normalize_execution_contract(execution_contract)
    for task in contract["task_graph"]:
        if task.get("task_id") == task_id:
            task["completed"] = True
            task["status"] = "completed"
            task["attempt_count"] = 0
            break
    return contract


def update_contract_task_attempt(
    execution_contract: dict[str, Any],
    task_id: str,
    attempt_count: int,
    *,
    status: str | None = None,
) -> dict[str, Any]:
    contract = normalize_execution_contract(execution_contract)
    for task in contract["task_graph"]:
        if task.get("task_id") == task_id:
            task["attempt_count"] = attempt_count
            if status:
                task["status"] = status
            break
    return contract


def sync_contract_with_plan(execution_contract: dict[str, Any], plan_md: str) -> dict[str, Any]:
    contract = normalize_execution_contract(execution_contract)
    parsed = parse_plan(plan_md)
    # Build title -> contract_task lookup for matching across reorderings
    contract_by_title: dict[str, dict[str, Any]] = {}
    for ct in contract["task_graph"]:
        title = ct.get("title", "")
        if title:
            contract_by_title[title] = ct
    for index, task in enumerate(parsed.tasks):
        stable_id = _stable_task_id(index, task.title)
        positional_id = f"task-{index + 1:03d}"
        completed = _is_task_completed(plan_md, task.line_number)
        # Match by title first, then by stable ID, then by positional ID
        matched = contract_by_title.get(task.title)
        if not matched:
            for contract_task in contract["task_graph"]:
                if contract_task.get("task_id") in (stable_id, positional_id):
                    matched = contract_task
                    break
        if matched:
            matched["completed"] = completed
            matched["status"] = "completed" if completed else matched.get("status", "pending")
    return contract


def append_plan_delta_history(
    plan_delta_history: list[dict[str, Any]] | None,
    plan_delta: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    history = list(plan_delta_history or [])
    if plan_delta:
        history.append(plan_delta)
    return history


def default_task_status_payload(execution_contract: dict[str, Any], active_task_id: str | None) -> dict[str, Any]:
    contract = normalize_execution_contract(execution_contract)
    active_task = None
    for task in contract.get("task_graph", []):
        if task.get("task_id") == active_task_id:
            active_task = task
            break
    completed, total = count_contract_progress(contract)
    return {
        "active_task_id": active_task_id,
        "active_task": active_task,
        "completed_tasks": completed,
        "total_tasks": total,
    }


def _default_validation_commands(
    runtime_spec: Any,
    stack_config: dict[str, Any],
    description: str,
) -> list[str]:
    commands: list[str] = []
    lowered = description.lower()
    if "build" in lowered or "page" in lowered or "ui" in lowered or runtime_spec.stack.startswith("nextjs"):
        if runtime_spec.build_command:
            commands.append(runtime_spec.build_command)
    if "prisma" in lowered or "database" in lowered or "schema" in lowered:
        if runtime_spec.prisma_smoke_command:
            commands.append(runtime_spec.prisma_smoke_command)
    if not commands and runtime_spec.build_command:
        commands.append(runtime_spec.build_command)
    return commands


def _build_done_when(description: str) -> list[str]:
    if not description.strip():
        return ["Task output exists and validation commands pass."]
    parts = [line.strip() for line in description.splitlines() if line.strip()]
    done_when = [parts[0]]
    for part in parts[1:]:
        if part.startswith("Context:"):
            continue
        done_when.append(part)
    return done_when or ["Task output exists and validation commands pass."]


def _is_task_completed(plan_md: str, line_number: int) -> bool:
    lines = plan_md.split("\n")
    if 1 <= line_number <= len(lines):
        return "[x]" in lines[line_number - 1].lower()
    return False


class _VirtualWorkspace:
    def __init__(self, markers: set[str]) -> None:
        self._markers = markers

    @classmethod
    def from_stack(cls, stack_config: dict[str, Any]) -> "_VirtualWorkspace":
        markers: set[str] = set()
        framework = json.dumps(stack_config, ensure_ascii=True).lower()
        if "next" in framework:
            markers.add("next.config.ts")
            markers.add("package-lock.json")
        if "vite" in framework:
            markers.add("vite.config.ts")
        if "python" in framework:
            markers.add("pyproject.toml")
        return cls(markers)

    def __truediv__(self, other: str) -> "_VirtualPath":
        return _VirtualPath(other in self._markers)


class _VirtualPath:
    def __init__(self, exists: bool) -> None:
        self._exists = exists

    def exists(self) -> bool:
        return self._exists
