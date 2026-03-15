"""Tests for execution contract generation and normalization."""

from orchestrator.execution_contract import (
    build_execution_contract,
    default_task_status_payload,
    get_next_contract_task,
    normalize_execution_contract,
)


SAMPLE_PLAN = """\
---
project_name: "MyApp"
stack: "Next.js + Prisma + SQLite"
root_dir: "/workspace"
global_rules:
  - "STRICT: Use TypeScript"
---

# План Реалізації: MyApp

## Фаза 1: Setup
- [ ] **Verify scaffold**: Confirm the existing Next.js scaffold is usable.
    - *Context*: Use the existing app router scaffold and keep the repo layout.
- [ ] **Add health endpoint**: Add /api/health with database probe.
    - *Context*: Create app/api/health/route.ts and return JSON.
"""


def test_build_execution_contract_from_plan():
    contract = build_execution_contract(
        project_name="MyApp",
        intake_text="Build a dashboard",
        plan_md=SAMPLE_PLAN,
        stack_config={
            "stack": "nextjs-prisma-sqlite-tailwind",
            "framework": "Next.js 15",
            "port": 3000,
            "package_manager": "npm",
        },
    )

    assert contract["project_meta"]["name"] == "MyApp"
    assert contract["stack_contract"]["framework"] == "Next.js 15"
    assert contract["validation_contract"]["health_path"] == "/api/health"
    assert len(contract["task_graph"]) == 2
    first_task = contract["task_graph"][0]
    # Task IDs now include a content hash for stability across re-planning
    assert first_task["task_id"].startswith("task-001-")
    assert first_task["done_when"]
    assert first_task["validation_commands"]
    assert first_task["allowed_autonomy"]
    assert first_task["escalate_if"]


def test_default_task_status_payload_uses_active_task():
    contract = normalize_execution_contract(
        {
            "task_graph": [
                {"task_id": "task-001", "completed": True, "status": "completed"},
                {"task_id": "task-002", "completed": False, "status": "pending"},
            ]
        }
    )

    payload = default_task_status_payload(contract, "task-002")

    assert payload["active_task_id"] == "task-002"
    assert payload["active_task"]["task_id"] == "task-002"
    assert payload["completed_tasks"] == 1
    assert payload["total_tasks"] == 2


def test_get_next_contract_task_skips_completed():
    contract = normalize_execution_contract(
        {
            "task_graph": [
                {"task_id": "task-001", "completed": True, "status": "completed"},
                {"task_id": "task-002", "completed": False, "status": "retrying"},
            ]
        }
    )

    next_task = get_next_contract_task(contract)

    assert next_task is not None
    assert next_task["task_id"] == "task-002"
