"""Validator node — runs validation_commands from the execution contract against the Docker container."""

from __future__ import annotations

import logging
import shlex
import uuid

from orchestrator.docker_engine.manager import ContainerManager
from orchestrator.execution_contract import normalize_execution_contract
from orchestrator.graph.state import AutomatronState
from orchestrator.observability import trace_event

logger = logging.getLogger(__name__)

container_manager = ContainerManager()

COMMAND_TIMEOUT = 360


def _render_validation_command(command: str) -> str:
    """Render shell-safe validation commands for nested bash execution.

    In particular, `node -e "..."` commands frequently contain `$disconnect()`
    and similar identifiers that get mangled by shell interpolation when routed
    through `bash -lc`. Materialize those snippets into a temporary file first.
    """

    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return command

    if len(argv) >= 3 and argv[0] == "node" and argv[1] == "-e":
        script = argv[2]
        script_path = f"/tmp/automatron-validate-{uuid.uuid4().hex}.js"
        return (
            f"cat > {script_path} <<'__AUTOMATRON_NODE__'\n"
            f"{script}\n"
            "__AUTOMATRON_NODE__\n"
            f"node {script_path}; "
            "EXIT_CODE=$?; "
            f"rm -f {script_path}; "
            "exit $EXIT_CODE"
        )

    return command


async def validator_node(state: AutomatronState) -> dict:
    """Run per-task validation_commands and gate builder output.

    Returns validation_gate_status:
      - "PASS"  — all commands succeeded (or none to run)
      - "FAIL"  — at least one command failed
      - "ERROR" — infrastructure error (container missing, exec failed)
    """
    container_id = state.get("container_id", "")
    active_task_id = state.get("active_task_id", "")
    execution_contract = normalize_execution_contract(state.get("execution_contract") or {})
    fast_retry_count = int(state.get("fast_retry_count", 0) or 0)

    # Find validation_commands for the active task
    task_contract = next(
        (t for t in execution_contract.get("task_graph", []) if t.get("task_id") == active_task_id),
        {},
    )
    validation_commands: list[str] = task_contract.get("validation_commands", [])

    # No commands to run — pass through
    if not validation_commands:
        await trace_event(
            state["project_id"],
            "validator",
            "validator.skipped",
            {"task_id": active_task_id, "reason": "no_validation_commands"},
            session_id=state.get("session_id"),
            stage=state.get("project_stage"),
        )
        return {
            "validation_gate_status": "PASS",
            "validation_command_results": [],
        }

    if not container_id:
        return {
            "validation_gate_status": "ERROR",
            "validation_command_results": [],
            "fast_retry_count": fast_retry_count,
        }

    results: list[dict] = []
    all_passed = True

    for cmd in validation_commands:
        rendered_command = _render_validation_command(cmd)
        await trace_event(
            state["project_id"],
            "validator",
            "validator.command.started",
            {"task_id": active_task_id, "command": cmd, "rendered_command": rendered_command},
            session_id=state.get("session_id"),
            stage=state.get("project_stage"),
        )
        try:
            exec_result = await container_manager.exec_in_container(
                container_id,
                f"cd /workspace && {rendered_command}",
                timeout=COMMAND_TIMEOUT,
            )
            result_entry = {
                "command": cmd,
                "exit_code": exec_result.exit_code,
                "output": exec_result.output[-3000:],
            }
            results.append(result_entry)
            await trace_event(
                state["project_id"],
                "validator",
                "validator.command.completed",
                {"task_id": active_task_id, **result_entry},
                session_id=state.get("session_id"),
                stage=state.get("project_stage"),
            )

            if exec_result.exit_code != 0:
                all_passed = False
                logger.info(
                    "Validation command failed for task %s: %s (exit %d)",
                    active_task_id,
                    cmd,
                    exec_result.exit_code,
                )
                break  # Early termination on first failure
        except Exception as exc:
            logger.error("Validation command execution error for task %s: %s", active_task_id, exc)
            return {
                "validation_gate_status": "ERROR",
                "validation_command_results": [
                    {"command": cmd, "exit_code": -1, "output": str(exc)[-3000:]},
                ],
                "fast_retry_count": fast_retry_count,
            }

    if all_passed:
        await trace_event(
            state["project_id"],
            "validator",
            "validator.completed",
            {"task_id": active_task_id, "gate_status": "PASS", "results": results},
            session_id=state.get("session_id"),
            stage="validating",
        )
        return {
            "validation_gate_status": "PASS",
            "validation_command_results": results,
        }

    await trace_event(
        state["project_id"],
        "validator",
        "validator.completed",
        {"task_id": active_task_id, "gate_status": "FAIL", "results": results},
        session_id=state.get("session_id"),
        stage="validating",
    )
    return {
        "validation_gate_status": "FAIL",
        "validation_command_results": results,
        "fast_retry_count": fast_retry_count + 1,
    }
