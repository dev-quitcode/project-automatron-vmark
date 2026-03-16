"""Docker container lifecycle manager."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shlex
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import docker
from docker.errors import DockerException, NotFound

from orchestrator.config import settings
from orchestrator.validation.runtime import PreviewRuntimeSpec, resolve_preview_runtime_spec

logger = logging.getLogger(__name__)


@dataclass
class ContainerInfo:
    """Information about a created container."""

    container_id: str
    name: str
    port: int
    status: str


@dataclass
class ExecResult:
    """Result of executing a command inside a container."""

    exit_code: int
    output: str


class ContainerManager:
    """Manages Docker container lifecycle for Automatron projects."""

    def __init__(self) -> None:
        try:
            self.client = docker.from_env()
            logger.info("Docker client initialized")
        except DockerException as e:
            logger.warning("Docker client initialization failed: %s", e)
            self.client = None  # type: ignore[assignment]

    @staticmethod
    async def _run_blocking(func, /, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    async def create_project_container(
        self,
        project_id: str,
        stack_config: dict,
        port: int,
    ) -> ContainerInfo:
        """Create and start a Docker container for a project.

        Args:
            project_id: Unique project identifier
            stack_config: Stack configuration from Architect
            port: External port to map

        Returns:
            ContainerInfo with container details
        """
        if not self.client:
            raise RuntimeError("Docker client not available")

        container_name = f"automatron-{project_id[:8]}"
        workspace_path = settings.workspace_base_dir / project_id
        workspace_path.mkdir(parents=True, exist_ok=True)
        image = settings.golden_image
        internal_port = stack_config.get("port", 3000)

        # Prepare environment variables (secrets injected at runtime)
        environment = {
            "PROJECT_ID": project_id,
            "TERM": "xterm-256color",
            "HOME": "/home/developer",
            "XDG_CONFIG_HOME": "/home/developer/.config",
        }

        # Inject API keys from settings (read from Docker Secrets at app start)
        if settings.openai_api_key:
            environment["OPENAI_API_KEY"] = settings.openai_api_key
        if settings.anthropic_api_key:
            environment["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        if settings.google_api_key:
            environment["GOOGLE_API_KEY"] = settings.google_api_key

        logger.info(
            "Creating container: name=%s, image=%s, port=%d:%d",
            container_name,
            image,
            port,
            internal_port,
        )

        try:
            try:
                existing = await self._run_blocking(self.client.containers.get, container_name)
            except NotFound:
                existing = None

            if existing is not None:
                logger.warning(
                    "Removing stale container with reused project name: %s (%s)",
                    container_name,
                    existing.id[:12],
                )
                await self._run_blocking(existing.remove, force=True)

            container = await self._run_blocking(
                self.client.containers.run,
                image=image,
                name=container_name,
                command="sleep infinity",  # Keep alive
                detach=True,
                user="developer",
                working_dir="/workspace",
                environment=environment,
                ports={f"{internal_port}/tcp": port},
                volumes={
                    str(workspace_path): {"bind": "/workspace", "mode": "rw"},
                },
                mem_limit="2g",
                cpu_period=100000,
                cpu_quota=100000,  # 1 CPU core
                restart_policy={"Name": "unless-stopped"},
            )

            # Windows bind mounts commonly surface as root-owned paths inside the
            # Linux container. Normalize ownership once so builder tasks can write
            # to /workspace without sudo workarounds.
            await self._run_blocking(
                container.exec_run,
                cmd=[
                    "bash",
                    "-lc",
                    (
                        "DEV_GROUP=$(id -gn developer 2>/dev/null || echo developer) && "
                        "chown -R developer:${DEV_GROUP} /workspace && "
                        "chmod -R u+rwX /workspace && "
                        "mkdir -p /home/developer/.config/prisma-nodejs && "
                        "chown -R developer:${DEV_GROUP} /home/developer"
                    ),
                ],
                user="root",
            )
            await self._run_blocking(
                container.exec_run,
                cmd=[
                    "bash",
                    "-lc",
                    "git config --global --add safe.directory /workspace",
                ],
                user="developer",
                environment={
                    "HOME": "/home/developer",
                    "XDG_CONFIG_HOME": "/home/developer/.config",
                },
            )

            logger.info(
                "Container created: id=%s, name=%s",
                container.id[:12],
                container_name,
            )

            return ContainerInfo(
                container_id=container.id,
                name=container_name,
                port=port,
                status="running",
            )

        except DockerException as e:
            logger.error("Failed to create container: %s", e)
            raise

    async def exec_in_container(
        self,
        container_id: str,
        command: str,
        timeout: int = 300,
    ) -> ExecResult:
        """Execute a command inside a running container.

        Args:
            container_id: Docker container ID
            command: Shell command to execute
            timeout: Timeout in seconds

        Returns:
            ExecResult with exit code and output
        """
        if not self.client:
            raise RuntimeError("Docker client not available")

        try:
            container = await self._run_blocking(self.client.containers.get, container_id)
            wrapped_command = command
            if timeout > 0:
                wrapped_command = (
                    f"timeout --signal=TERM {timeout}s "
                    f"bash -lc {shlex.quote(command)}"
                )
            exec_result = await self._run_blocking(
                container.exec_run,
                cmd=["bash", "-lc", wrapped_command],
                workdir="/workspace",
                user="developer",
                demux=True,
            )

            stdout = (exec_result.output[0] or b"").decode("utf-8", errors="replace")
            stderr = (exec_result.output[1] or b"").decode("utf-8", errors="replace")
            output = stdout + ("\n--- STDERR ---\n" + stderr if stderr else "")

            return ExecResult(
                exit_code=exec_result.exit_code,
                output=output,
            )

        except NotFound:
            raise RuntimeError(f"Container {container_id[:12]} not found")
        except DockerException as e:
            logger.error("Exec failed in container %s: %s", container_id[:12], e)
            raise

    async def stop_container(self, container_id: str) -> None:
        """Stop a running container."""
        if not self.client:
            return
        try:
            container = await self._run_blocking(self.client.containers.get, container_id)
            await self._run_blocking(container.stop, timeout=10)
            logger.info("Container %s stopped", container_id[:12])
        except NotFound:
            logger.warning("Container %s not found", container_id[:12])
        except DockerException as e:
            logger.error("Failed to stop container %s: %s", container_id[:12], e)

    async def restart_container(self, container_id: str) -> None:
        """Restart a container."""
        if not self.client:
            return
        try:
            container = await self._run_blocking(self.client.containers.get, container_id)
            await self._run_blocking(container.restart, timeout=10)
            logger.info("Container %s restarted", container_id[:12])
        except DockerException as e:
            logger.error("Failed to restart container %s: %s", container_id[:12], e)

    async def remove_container(self, container_id: str) -> None:
        """Remove a container (force stop + remove)."""
        if not self.client:
            return
        try:
            container = await self._run_blocking(self.client.containers.get, container_id)
            await self._run_blocking(container.remove, force=True)
            logger.info("Container %s removed", container_id[:12])
        except NotFound:
            pass
        except DockerException as e:
            logger.error("Failed to remove container %s: %s", container_id[:12], e)

    async def get_container_logs(
        self, container_id: str, tail: int = 100
    ) -> str:
        """Get container logs."""
        if not self.client:
            return ""
        try:
            container = await self._run_blocking(self.client.containers.get, container_id)
            raw_logs = await self._run_blocking(container.logs, tail=tail)
            logs = raw_logs.decode("utf-8", errors="replace")
            return logs
        except DockerException as e:
            logger.error("Failed to get logs from %s: %s", container_id[:12], e)
            return ""

    async def copy_file_to_container(
        self,
        container_id: str,
        content: str,
        container_path: str,
    ) -> None:
        """Copy file content into a container.

        Creates a tar archive in memory and puts it into the container.
        """
        if not self.client:
            return

        try:
            container = await self._run_blocking(self.client.containers.get, container_id)

            # Build tar archive in memory
            data = content.encode("utf-8")
            tarstream = io.BytesIO()
            with tarfile.open(fileobj=tarstream, mode="w") as tar:
                info = tarfile.TarInfo(name=container_path.split("/")[-1])
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            tarstream.seek(0)

            # Put archive into container
            path_dir = "/".join(container_path.split("/")[:-1])
            await self._run_blocking(container.put_archive, path_dir, tarstream)

            logger.debug("Copied file to %s:%s", container_id[:12], container_path)

        except DockerException as e:
            logger.error(
                "Failed to copy file to %s:%s: %s",
                container_id[:12],
                container_path,
                e,
            )
            raise

    async def read_file_from_container(
        self, container_id: str, container_path: str
    ) -> str:
        """Read a file from inside a container."""
        result = await self.exec_in_container(
            container_id, f"cat {container_path}", timeout=10
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"Failed to read {container_path}: {result.output}"
            )
        return result.output

    async def start_preview_process(
        self,
        container_id: str,
        *,
        internal_port: int,
        external_port: int,
        stack_config: dict,
        workspace_path: Path,
        restart_reason: str = "preview_check",
        runtime_spec: PreviewRuntimeSpec | None = None,
    ) -> dict[str, str]:
        spec = runtime_spec or resolve_preview_runtime_spec(workspace_path, stack_config)
        command = f"{spec.install_command} && {spec.render_preview_command(internal_port)}"
        cleanup_command = self._preview_cleanup_command(spec, internal_port)
        wrapped_command = (
            f"{cleanup_command} "
            f"&& nohup bash -lc {self._quote_for_bash(command)} "
            "> /tmp/automatron-preview.log 2>&1 & "
            "PID=$!; "
            "echo $PID > /tmp/automatron-preview.pid 2>/dev/null || true; "
            "echo $PID"
        )
        exec_result = await self.exec_in_container(container_id, wrapped_command, timeout=20)
        pid = self._extract_preview_pid(exec_result.output)
        pid_file_present = False
        if not pid:
            try:
                pid = (await self.read_file_from_container(container_id, "/tmp/automatron-preview.pid")).strip()
                pid_file_present = bool(pid)
            except RuntimeError:
                pid = ""
        else:
            pid_file_present = True
        metadata = {
            "pid": pid,
            "command": command,
            "started_at": _now(),
            "restart_reason": restart_reason,
            "probe_url": spec.probe_url(internal_port),
            "runtime_stack": spec.stack,
            "pid_file_present": "true" if pid_file_present else "false",
            "startup_output": exec_result.output[-1000:],
        }
        logger.info(
            "Started preview process for %s on %d (host %d)",
            container_id[:12],
            internal_port,
            external_port,
        )
        return metadata

    async def wait_for_preview(
        self,
        container_id: str,
        *,
        internal_port: int,
        probe_path: str = "/",
        attempts: int = 20,
        delay_seconds: int = 2,
    ) -> None:
        check_command = (
            f"for i in $(seq 1 {attempts}); do "
            f"curl -fsS http://127.0.0.1:{internal_port}{probe_path} >/dev/null && exit 0; "
            f"sleep {delay_seconds}; "
            "done; "
            "echo 'Preview server failed to respond'; "
            "exit 1"
        )
        result = await self.exec_in_container(container_id, check_command, timeout=attempts * delay_seconds + 10)
        if result.exit_code != 0:
            raise RuntimeError(result.output)

    @staticmethod
    def _quote_for_bash(command: str) -> str:
        escaped = command.replace("\\", "\\\\").replace("\"", "\\\"")
        return f'"{escaped}"'

    @staticmethod
    def _preview_cleanup_command(runtime_spec: PreviewRuntimeSpec, internal_port: int) -> str:
        commands = [
            "if [ -f /tmp/automatron-preview.pid ]; then PID=$(cat /tmp/automatron-preview.pid); kill \"$PID\" >/dev/null 2>&1 || true; wait \"$PID\" >/dev/null 2>&1 || true; fi",
            "rm -f /tmp/automatron-preview.pid /tmp/automatron-preview.log",
        ]
        commands.extend(ContainerManager._preview_process_kill_commands(runtime_spec, internal_port))
        commands.extend(f"rm -rf /workspace/{cache_dir}" for cache_dir in runtime_spec.cache_dirs)
        return "; ".join(command for command in commands if command)

    @staticmethod
    def _extract_preview_pid(output: str) -> str:
        for line in reversed((output or "").splitlines()):
            candidate = line.strip()
            if re.fullmatch(r"\d+", candidate):
                return candidate
        return ""

    @staticmethod
    def _preview_process_kill_commands(runtime_spec: PreviewRuntimeSpec, internal_port: int) -> list[str]:
        if runtime_spec.stack.startswith("nextjs"):
            return [
                f"pkill -f 'next dev --hostname 0.0.0.0 --port {internal_port}' >/dev/null 2>&1 || true",
                f"pkill -f 'npm run dev -- --hostname 0.0.0.0 --port {internal_port}' >/dev/null 2>&1 || true",
                f"pkill -f 'next start --port {internal_port}' >/dev/null 2>&1 || true",
            ]
        return []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
