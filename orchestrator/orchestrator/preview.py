"""Local preview runner — clones a GitHub repo and runs it in Docker."""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import subprocess
from pathlib import Path

import httpx

from orchestrator.config import settings

logger = logging.getLogger(__name__)


# ── Port helpers ──────────────────────────────────────────────────────────────

def _get_used_host_ports() -> set[int]:
    """Return set of host ports currently bound by any Docker container."""
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        used: set[int] = set()
        for container in client.containers.list():
            for bindings in (container.ports or {}).values():
                for b in (bindings or []):
                    try:
                        used.add(int(b["HostPort"]))
                    except (KeyError, ValueError, TypeError):
                        pass
        client.close()
        return used
    except Exception:
        return set()


def _find_free_port() -> int:
    used = _get_used_host_ports()
    for port in range(settings.port_range_start, settings.port_range_end + 1):
        if port not in used:
            return port
    raise RuntimeError("No free port available in configured range")


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


# ── Project type detection ────────────────────────────────────────────────────

def _detect_project_type(repo_dir: Path) -> str:
    pkg = repo_dir / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            if "next" in deps:
                return "nextjs"
            if "vite" in deps or "@vitejs/plugin-react" in deps:
                return "vite"
        except Exception:
            pass
        return "node"
    if (repo_dir / "pyproject.toml").exists() or (repo_dir / "requirements.txt").exists():
        return "python"
    return "unknown"


def _ensure_dockerfile(repo_dir: Path, project_type: str) -> None:
    """Write a minimal Dockerfile if one doesn't already exist."""
    dockerfile = repo_dir / "Dockerfile"
    if dockerfile.exists():
        return

    if project_type == "nextjs":
        dockerfile.write_text(
            "FROM node:22-alpine\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN npm install\n"
            "RUN npm run build\n"
            "EXPOSE 3000\n"
            'CMD ["npm", "start"]\n'
        )
    elif project_type == "vite":
        dockerfile.write_text(
            "FROM node:22-alpine\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN npm install\n"
            "RUN npm run build\n"
            "RUN npm install -g serve\n"
            "EXPOSE 3000\n"
            'CMD ["serve", "-s", "dist", "-l", "3000"]\n'
        )
    elif project_type == "node":
        dockerfile.write_text(
            "FROM node:22-alpine\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN npm install\n"
            "EXPOSE 3000\n"
            'CMD ["npm", "start"]\n'
        )
    elif project_type == "python":
        dockerfile.write_text(
            "FROM python:3.12-slim\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || "
            "pip install --no-cache-dir -e . 2>/dev/null || true\n"
            "EXPOSE 8000\n"
            'CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]\n'
        )


def _detect_internal_port(repo_dir: Path) -> int:
    dockerfile = repo_dir / "Dockerfile"
    if dockerfile.exists():
        for line in dockerfile.read_text().splitlines():
            if line.strip().upper().startswith("EXPOSE"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return int(parts[1])
                    except ValueError:
                        pass
    return 3000


# ── Main entry point ─────────────────────────────────────────────────────────

async def run_preview_locally(
    project_id: str, owner: str, repo: str, default_branch: str = "main"
) -> str | None:
    """Clone the repo, build, and run it in Docker. Returns the preview URL or None."""
    workspace = settings.workspace_base_dir / str(project_id)
    workspace.mkdir(parents=True, exist_ok=True)
    # Use a separate directory from the aider workspace to avoid branch conflicts
    repo_dir = workspace / "preview-repo"

    token = settings.github_token
    clone_url = (
        f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
        if token
        else f"https://github.com/{owner}/{repo}.git"
    )

    if (repo_dir / ".git").exists():
        logger.info("Preview: syncing %s/%s to %s", owner, repo, default_branch)
        _run(["git", "remote", "set-url", "origin", clone_url], cwd=repo_dir)
        _run(["git", "fetch", "origin"], cwd=repo_dir)
        _run(["git", "checkout", default_branch], cwd=repo_dir)
        rc, out = _run(["git", "reset", "--hard", f"origin/{default_branch}"], cwd=repo_dir)
    else:
        logger.info("Preview: cloning %s/%s", owner, repo)
        rc, out = _run(["git", "clone", clone_url, str(repo_dir)])

    if rc != 0:
        logger.error("Preview: git failed:\n%s", out)
        return None

    project_type = _detect_project_type(repo_dir)
    logger.info("Preview: detected project type=%s for %s/%s", project_type, owner, repo)

    if project_type == "unknown":
        logger.warning("Preview: unrecognised project type for %s/%s", owner, repo)
        return None

    _ensure_dockerfile(repo_dir, project_type)

    port = _find_free_port()
    container_name = f"preview-{project_id}"
    image_name = f"automatron-preview-{project_id}"

    import docker as docker_sdk
    client = docker_sdk.from_env()
    try:
        # Stop any existing container for this project
        try:
            old = client.containers.get(container_name)
            old.remove(force=True)
            logger.info("Preview: removed old container %s", container_name)
        except docker_sdk.errors.NotFound:
            pass

        # Build
        logger.info("Preview: building image %s", image_name)
        try:
            _, build_logs = client.images.build(path=str(repo_dir), tag=image_name, rm=True)
            for chunk in build_logs:
                if "stream" in chunk:
                    line = chunk["stream"].rstrip()
                    if line:
                        logger.debug("Preview build: %s", line)
        except docker_sdk.errors.BuildError as exc:
            build_output = "\n".join(
                chunk.get("stream", chunk.get("error", "")).rstrip()
                for chunk in exc.build_log
                if chunk.get("stream") or chunk.get("error")
            )
            logger.error("Preview: docker build failed:\n%s", build_output[-3000:])
            return None

        internal_port = _detect_internal_port(repo_dir)

        # Run
        try:
            client.containers.run(
                image_name,
                detach=True,
                name=container_name,
                ports={f"{internal_port}/tcp": port},
                restart_policy={"Name": "unless-stopped"},
            )
        except Exception as exc:
            logger.error("Preview: docker run failed: %s", exc)
            return None
    finally:
        try:
            client.close()
        except Exception as exc:
            logger.warning("Preview: docker client close failed: %s", exc)

    # Public URL shown to users
    from urllib.parse import urlparse
    public = (settings.automatron_public_url or "").rstrip("/")
    if public:
        host = urlparse(public).hostname or "localhost"
    else:
        host = "localhost"
    preview_url = f"http://{host}:{port}"

    # Health-check using localhost — containers can't reach the public hostname via hairpin NAT
    health_url = f"http://localhost:{port}"
    logger.info("Preview: container started, polling %s", health_url)

    for attempt in range(20):
        await asyncio.sleep(3)
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(health_url)
                if resp.status_code < 500:
                    logger.info("Preview: ready at %s (attempt %d)", preview_url, attempt + 1)
                    return preview_url
        except Exception:
            pass

    logger.warning("Preview: health check timed out, returning URL anyway: %s", preview_url)
    return preview_url
