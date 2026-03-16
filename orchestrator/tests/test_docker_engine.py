"""Tests for Docker engine (mocked)."""

import pytest

from orchestrator.docker_engine.manager import ContainerManager, ExecResult
from orchestrator.docker_engine.port_allocator import PortAllocator


def test_port_free_check():
    """Test that _is_port_free works for high ports."""
    allocator = PortAllocator(start=49000, end=49010)
    # Very high ports should generally be free
    assert allocator._is_port_free(49999) is True


@pytest.mark.asyncio
async def test_start_preview_process_uses_runtime_spec_and_tracks_metadata(tmp_path):
    workspace = tmp_path
    (workspace / "next.config.ts").write_text("export default {};\n", encoding="utf-8")
    (workspace / "package-lock.json").write_text("{}\n", encoding="utf-8")

    manager = ContainerManager.__new__(ContainerManager)
    commands: list[str] = []

    async def fake_exec(container_id: str, command: str, timeout: int = 300) -> ExecResult:
        commands.append(command)
        return ExecResult(exit_code=0, output="")

    async def fake_read(container_id: str, container_path: str) -> str:
        return "4242\n"

    manager.exec_in_container = fake_exec  # type: ignore[method-assign]
    manager.read_file_from_container = fake_read  # type: ignore[method-assign]

    metadata = await manager.start_preview_process(
        "container-1",
        internal_port=3000,
        external_port=7001,
        stack_config={"framework": "nextjs"},
        workspace_path=workspace,
    )

    assert any("rm -rf /workspace/.next" in command for command in commands)
    assert any("pkill -f 'next start --port 3000'" in command for command in commands)
    assert any("pkill -f 'next dev --hostname 0.0.0.0 --port 3000'" in command for command in commands)
    assert all("; &&" not in command for command in commands)
    assert metadata["pid"] == "4242"
    assert metadata["probe_url"] == "http://127.0.0.1:3000/api/health"
    assert "npm ci || npm install" in metadata["command"]
    assert "npm run dev -- --hostname 0.0.0.0 --port 3000" in metadata["command"]


@pytest.mark.asyncio
async def test_start_preview_process_tolerates_missing_pid_file(tmp_path):
    workspace = tmp_path
    (workspace / "next.config.ts").write_text("export default {};\n", encoding="utf-8")
    (workspace / "package-lock.json").write_text("{}\n", encoding="utf-8")

    manager = ContainerManager.__new__(ContainerManager)

    async def fake_exec(container_id: str, command: str, timeout: int = 300) -> ExecResult:
        return ExecResult(exit_code=0, output="")

    async def fake_read(container_id: str, container_path: str) -> str:
        raise RuntimeError("missing pid file")

    manager.exec_in_container = fake_exec  # type: ignore[method-assign]
    manager.read_file_from_container = fake_read  # type: ignore[method-assign]

    metadata = await manager.start_preview_process(
        "container-1",
        internal_port=3000,
        external_port=7001,
        stack_config={"framework": "nextjs"},
        workspace_path=workspace,
    )

    assert metadata["pid"] == ""
    assert metadata["pid_file_present"] == "false"
    assert metadata["probe_url"] == "http://127.0.0.1:3000/api/health"


@pytest.mark.asyncio
async def test_wait_for_preview_uses_probe_path():
    manager = ContainerManager.__new__(ContainerManager)
    commands: list[str] = []

    async def fake_exec(container_id: str, command: str, timeout: int = 300) -> ExecResult:
        commands.append(command)
        return ExecResult(exit_code=0, output="")

    manager.exec_in_container = fake_exec  # type: ignore[method-assign]

    await manager.wait_for_preview(
        "container-1",
        internal_port=3000,
        probe_path="/api/health",
        attempts=1,
        delay_seconds=1,
    )

    assert commands
    assert "http://127.0.0.1:3000/api/health" in commands[0]
