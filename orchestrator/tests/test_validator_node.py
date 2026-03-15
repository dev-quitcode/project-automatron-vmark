"""Tests for the validator node and route_after_validator edge."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.graph.edges import MAX_FAST_RETRIES, route_after_validator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeExecResult:
    exit_code: int
    output: str


def _make_state(**overrides):
    state = {
        "project_id": "test-123",
        "container_id": "container-abc",
        "active_task_id": "task-001-abc123",
        "fast_retry_count": 0,
        "validation_gate_status": "",
        "validation_command_results": [],
        "execution_contract": {
            "task_graph": [
                {
                    "task_id": "task-001-abc123",
                    "title": "Build the app",
                    "validation_commands": ["npm run build"],
                    "completed": False,
                    "attempt_count": 0,
                    "status": "pending",
                }
            ],
            "escalation_policy": {"self_retries": 2},
        },
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# route_after_validator tests
# ---------------------------------------------------------------------------


class TestRouteAfterValidator:
    def test_pass_routes_to_status_classifier(self):
        state = _make_state(validation_gate_status="PASS")
        assert route_after_validator(state) == "status_classifier"

    def test_fail_within_limit_routes_to_builder(self):
        state = _make_state(validation_gate_status="FAIL", fast_retry_count=1)
        assert route_after_validator(state) == "builder"

    def test_fail_at_limit_routes_to_builder(self):
        state = _make_state(validation_gate_status="FAIL", fast_retry_count=MAX_FAST_RETRIES)
        assert route_after_validator(state) == "builder"

    def test_fail_over_limit_routes_to_status_classifier(self):
        state = _make_state(validation_gate_status="FAIL", fast_retry_count=MAX_FAST_RETRIES + 1)
        assert route_after_validator(state) == "status_classifier"

    def test_error_routes_to_status_classifier(self):
        state = _make_state(validation_gate_status="ERROR")
        assert route_after_validator(state) == "status_classifier"

    def test_empty_status_defaults_to_pass(self):
        state = _make_state(validation_gate_status="")
        assert route_after_validator(state) == "status_classifier"


# ---------------------------------------------------------------------------
# validator_node tests
# ---------------------------------------------------------------------------


class TestValidatorNode:
    @pytest.mark.asyncio
    async def test_pass_through_when_no_commands(self):
        from orchestrator.graph.nodes.validator import validator_node

        state = _make_state(
            execution_contract={
                "task_graph": [
                    {
                        "task_id": "task-001-abc123",
                        "title": "No validation",
                        "validation_commands": [],
                        "completed": False,
                        "attempt_count": 0,
                        "status": "pending",
                    }
                ],
                "escalation_policy": {"self_retries": 2},
            }
        )
        result = await validator_node(state)
        assert result["validation_gate_status"] == "PASS"
        assert result["validation_command_results"] == []

    @pytest.mark.asyncio
    async def test_all_commands_pass(self):
        from orchestrator.graph.nodes.validator import validator_node

        fake_exec = AsyncMock(return_value=_FakeExecResult(exit_code=0, output="Build OK"))

        with patch("orchestrator.graph.nodes.validator.container_manager") as mock_cm:
            mock_cm.exec_in_container = fake_exec
            result = await validator_node(_make_state())

        assert result["validation_gate_status"] == "PASS"
        assert len(result["validation_command_results"]) == 1
        assert result["validation_command_results"][0]["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_command_failure_returns_fail(self):
        from orchestrator.graph.nodes.validator import validator_node

        fake_exec = AsyncMock(
            return_value=_FakeExecResult(exit_code=1, output="Error: something broke")
        )

        with patch("orchestrator.graph.nodes.validator.container_manager") as mock_cm:
            mock_cm.exec_in_container = fake_exec
            result = await validator_node(_make_state())

        assert result["validation_gate_status"] == "FAIL"
        assert result["fast_retry_count"] == 1
        assert result["validation_command_results"][0]["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_early_termination_on_first_failure(self):
        from orchestrator.graph.nodes.validator import validator_node

        call_count = 0

        async def fake_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _FakeExecResult(exit_code=1, output="fail")

        state = _make_state(
            execution_contract={
                "task_graph": [
                    {
                        "task_id": "task-001-abc123",
                        "title": "Multi-command",
                        "validation_commands": ["cmd1", "cmd2", "cmd3"],
                        "completed": False,
                        "attempt_count": 0,
                        "status": "pending",
                    }
                ],
                "escalation_policy": {"self_retries": 2},
            }
        )

        with patch("orchestrator.graph.nodes.validator.container_manager") as mock_cm:
            mock_cm.exec_in_container = fake_exec
            result = await validator_node(state)

        assert call_count == 1  # Stopped after first failure
        assert result["validation_gate_status"] == "FAIL"
        assert len(result["validation_command_results"]) == 1

    @pytest.mark.asyncio
    async def test_no_container_returns_error(self):
        from orchestrator.graph.nodes.validator import validator_node

        state = _make_state(container_id="")
        result = await validator_node(state)
        assert result["validation_gate_status"] == "ERROR"

    @pytest.mark.asyncio
    async def test_exec_exception_returns_error(self):
        from orchestrator.graph.nodes.validator import validator_node

        async def fail_exec(*args, **kwargs):
            raise RuntimeError("Container not found")

        with patch("orchestrator.graph.nodes.validator.container_manager") as mock_cm:
            mock_cm.exec_in_container = fail_exec
            result = await validator_node(_make_state())

        assert result["validation_gate_status"] == "ERROR"
        assert "Container not found" in result["validation_command_results"][0]["output"]

    @pytest.mark.asyncio
    async def test_fast_retry_count_increments(self):
        from orchestrator.graph.nodes.validator import validator_node

        fake_exec = AsyncMock(return_value=_FakeExecResult(exit_code=1, output="fail"))
        state = _make_state(fast_retry_count=1)

        with patch("orchestrator.graph.nodes.validator.container_manager") as mock_cm:
            mock_cm.exec_in_container = fake_exec
            result = await validator_node(state)

        assert result["fast_retry_count"] == 2

    @pytest.mark.asyncio
    async def test_node_e_validation_commands_are_materialized_to_temp_file(self):
        from orchestrator.graph.nodes.validator import validator_node

        captured_commands: list[str] = []

        async def fake_exec(container_id: str, command: str, timeout: int = 300):
            captured_commands.append(command)
            return _FakeExecResult(exit_code=0, output="ok")

        state = _make_state(
            execution_contract={
                "task_graph": [
                    {
                        "task_id": "task-001-abc123",
                        "title": "Prisma smoke",
                        "validation_commands": [
                            'node -e "const { PrismaClient } = require(\'@prisma/client\'); const client = new PrismaClient(); Promise.resolve(client.$disconnect()).then(() => process.exit(0));"'
                        ],
                        "completed": False,
                        "attempt_count": 0,
                        "status": "pending",
                    }
                ],
                "escalation_policy": {"self_retries": 2},
            }
        )

        with patch("orchestrator.graph.nodes.validator.container_manager") as mock_cm:
            mock_cm.exec_in_container = fake_exec
            result = await validator_node(state)

        assert result["validation_gate_status"] == "PASS"
        assert captured_commands
        assert "cat > /tmp/automatron-validate-" in captured_commands[0]
        assert "node /tmp/automatron-validate-" in captured_commands[0]
