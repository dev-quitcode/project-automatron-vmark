"""Run npm run build (or equivalent) against a repo after a PR merges."""
from __future__ import annotations

import logging
from pathlib import Path

from orchestrator.config import settings

logger = logging.getLogger(__name__)


def _sync_run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    import subprocess
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _detect_build_command(repo_dir: Path) -> list[str] | None:
    """Return the build command for the project, or None if not applicable."""
    pkg = repo_dir / "package.json"
    if pkg.exists():
        import json
        try:
            data = json.loads(pkg.read_text())
            scripts = data.get("scripts", {})
            if "build" in scripts:
                return ["sh", "-c", "npm install --prefer-offline --no-audit && npm run build"]
        except Exception:
            pass
        return None
    return None


async def run_project_build_check(
    project_id: str,
    owner: str,
    repo: str,
    default_branch: str = "main",
) -> None:
    """Run npm run build on the default branch and log the result to activity_logs."""
    from orchestrator.models.project import save_activity_log, get_activity_logs
    from orchestrator.api.websocket import emit_error, emit_status_update
    import uuid as _uuid

    workspace = settings.workspace_base_dir / str(project_id)
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = workspace / "build-check-repo"

    token = settings.github_token
    clone_url = (
        f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
        if token
        else f"https://github.com/{owner}/{repo}.git"
    )

    # Compute next seq for activity log
    existing = await get_activity_logs(project_id)
    seq = (max((r.get("seq", 0) for r in existing), default=0) + 1)

    await save_activity_log(project_id, seq, f"Build check: {owner}/{repo} @ {default_branch}", "", "INFO")
    seq += 1

    if (repo_dir / ".git").exists():
        _sync_run(["git", "remote", "set-url", "origin", clone_url], cwd=repo_dir)
        _sync_run(["git", "fetch", "origin"], cwd=repo_dir)
        _sync_run(["git", "checkout", default_branch], cwd=repo_dir)
        rc, out = _sync_run(["git", "reset", "--hard", f"origin/{default_branch}"], cwd=repo_dir)
    else:
        rc, out = _sync_run(["git", "clone", clone_url, str(repo_dir)])

    if rc != 0:
        await save_activity_log(project_id, seq, "Build check: git failed", out[-500:], "ERROR")
        await emit_error(project_id, f"Build check: git failed — {out[-200:]}")
        return

    build_cmd = _detect_build_command(repo_dir)
    if build_cmd is None:
        await save_activity_log(project_id, seq, "Build check: no build script found — skipped", "", "INFO")
        return

    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        logger.info("Build check (project): running npm run build for %s/%s", owner, repo)

        container = client.containers.run(
            "node:20-alpine",
            command=build_cmd,
            volumes={str(repo_dir.resolve()): {"bind": "/app", "mode": "rw"}},
            working_dir="/app",
            remove=True,
            stdout=True,
            stderr=True,
            environment={"NODE_ENV": "production", "CI": "1", "NEXT_TELEMETRY_DISABLED": "1"},
        )
        output = container.decode(errors="replace") if isinstance(container, bytes) else str(container)
        await save_activity_log(project_id, seq, "Build check: PASSED", output[-2000:], "INFO")
        logger.info("Build check (project): PASSED for %s/%s", owner, repo)

    except Exception as exc:
        import docker as docker_sdk
        if isinstance(exc, docker_sdk.errors.ContainerError):
            stderr = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            detail = stderr[-2000:]
        else:
            detail = str(exc)
        await save_activity_log(project_id, seq, "Build check: FAILED", detail, "ERROR")
        await emit_error(project_id, f"Build check failed — see Activity tab for details")
        logger.error("Build check (project): FAILED for %s/%s: %s", owner, repo, detail[-200:])


async def run_build_check(
    project_id: str,
    owner: str,
    repo: str,
    issue_number: int,
    default_branch: str = "main",
) -> bool:
    """Clone/sync repo, run npm run build in Docker, return True if it passes."""
    from orchestrator.models.project import update_github_issue_build_status, list_github_issues
    from orchestrator.api.websocket import emit_issues_updated

    workspace = settings.workspace_base_dir / str(project_id)
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = workspace / "build-check-repo"

    token = settings.github_token
    clone_url = (
        f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
        if token
        else f"https://github.com/{owner}/{repo}.git"
    )

    # Mark as running
    await update_github_issue_build_status(project_id, issue_number, "running")
    await emit_issues_updated(project_id, await list_github_issues(project_id))

    # Clone or sync to default branch
    if (repo_dir / ".git").exists():
        _sync_run(["git", "remote", "set-url", "origin", clone_url], cwd=repo_dir)
        _sync_run(["git", "fetch", "origin"], cwd=repo_dir)
        _sync_run(["git", "checkout", default_branch], cwd=repo_dir)
        rc, out = _sync_run(["git", "reset", "--hard", f"origin/{default_branch}"], cwd=repo_dir)
    else:
        rc, out = _sync_run(["git", "clone", clone_url, str(repo_dir)])

    if rc != 0:
        logger.error("Build check: git failed for %s/%s:\n%s", owner, repo, out)
        await update_github_issue_build_status(project_id, issue_number, "failed")
        await emit_issues_updated(project_id, await list_github_issues(project_id))
        return False

    build_cmd = _detect_build_command(repo_dir)
    if build_cmd is None:
        logger.info("Build check: no build command detected for %s/%s — skipping", owner, repo)
        await update_github_issue_build_status(project_id, issue_number, "passed")
        await emit_issues_updated(project_id, await list_github_issues(project_id))
        return True

    # Run build inside a Node.js Docker container
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        logger.info("Build check: running npm run build for %s/%s issue #%d", owner, repo, issue_number)

        container = client.containers.run(
            "node:20-alpine",
            command=build_cmd,
            volumes={str(repo_dir.resolve()): {"bind": "/app", "mode": "rw"}},
            working_dir="/app",
            remove=True,
            stdout=True,
            stderr=True,
            environment={"NODE_ENV": "production", "CI": "1", "NEXT_TELEMETRY_DISABLED": "1"},
        )
        output = container.decode(errors="replace") if isinstance(container, bytes) else str(container)
        logger.info("Build check: PASSED for %s/%s issue #%d", owner, repo, issue_number)
        logger.debug("Build output:\n%s", output[-2000:])
        passed = True

    except Exception as exc:
        # ContainerError means the build command exited non-zero
        import docker as docker_sdk
        if isinstance(exc, docker_sdk.errors.ContainerError):
            stderr = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            logger.error("Build check: FAILED for %s/%s issue #%d\n%s", owner, repo, issue_number, stderr[-2000:])
        else:
            logger.error("Build check: error for %s/%s issue #%d: %s", owner, repo, issue_number, exc)
        passed = False

    status = "passed" if passed else "failed"
    await update_github_issue_build_status(project_id, issue_number, status)
    await emit_issues_updated(project_id, await list_github_issues(project_id))
    return passed
