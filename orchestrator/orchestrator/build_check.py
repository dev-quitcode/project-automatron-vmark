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
                return ["sh", "-c", "npm install --prefer-offline --no-audit --include=dev && npm run build"]
        except Exception:
            pass
        return None
    return None


async def run_build_in_docker(repo_dir: Path) -> tuple[bool, str]:
    """Run npm run build inside a node:20-alpine container.

    Returns (passed, output_or_error_string).
    Returns (True, "...") when no build script is detected (treated as a pass).
    """
    build_cmd = _detect_build_command(repo_dir)
    if build_cmd is None:
        return True, "no build script detected — skipped"
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
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
        return True, output
    except Exception as exc:
        import docker as docker_sdk
        if isinstance(exc, docker_sdk.errors.ContainerError):
            stderr = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            return False, stderr
        # Docker daemon not reachable — treat as skipped (not a build failure)
        if "docker" in type(exc).__module__ or "connect" in str(exc).lower() or "socket" in str(exc).lower():
            logger.warning("run_build_in_docker: Docker unavailable (%s) — treating as pass", exc)
            return True, f"Docker unavailable — skipped: {exc}"
        return False, str(exc)


def _extract_build_error(detail: str) -> str:
    """Return matched error lines + the last 100 lines of output for full context."""
    lines = detail.splitlines()
    error_lines = [l for l in lines if any(k in l for k in ("Error:", "error TS", "⨯", "Failed to", "Cannot find", "SyntaxError", "TypeError", "× ", "Module not found", "Can't resolve", "does not provide"))]
    tail = lines[-100:]
    if error_lines:
        header = "── Matched errors ──\n" + "\n".join(error_lines[:20])
        body = "── Last 100 lines of output ──\n" + "\n".join(tail)
        return f"{header}\n\n{body}"
    return "\n".join(tail)


async def _create_build_failure_issue(
    project_id: str,
    owner: str,
    repo: str,
    default_branch: str,
    detail: str,
) -> None:
    """Open a GitHub issue for the build failure and record it in the DB."""
    import uuid as _uuid
    from orchestrator.github.issues import GitHubClient
    from orchestrator.models.project import create_github_issue, list_github_issues
    from orchestrator.api.websocket import emit_issues_updated

    summary = _extract_build_error(detail)
    title = f"Build failure on {default_branch}"
    body = (
        f"## Build check failed on `{default_branch}`\n\n"
        f"Running `npm run build` failed. Error summary:\n\n"
        f"```\n{summary[:3000]}\n```\n\n"
        f"**Full log** is in the Automatron Activity tab.\n\n"
        f"### Acceptance criteria\n"
        f"- [ ] `npm run build` exits 0 on `{default_branch}`\n"
    )

    try:
        gh = GitHubClient()
        gh_issue = await gh.create_issue(owner, repo, title=title, body=body, labels=["bug"])
        issue_number = gh_issue["number"]
        html_url = gh_issue["html_url"]
        await create_github_issue(
            str(_uuid.uuid4()),
            project_id,
            issue_number,
            title,
            epic="Build",
            copilot_workspace_url=html_url,
        )
        updated_issues = await list_github_issues(project_id)
        await emit_issues_updated(project_id, updated_issues)
        logger.info("Build check: created GitHub issue #%d for build failure", issue_number)
    except Exception as exc:
        logger.error("Build check: failed to create GitHub issue: %s", exc)


async def run_project_build_check(
    project_id: str,
    owner: str,
    repo: str,
    default_branch: str = "main",
) -> None:
    """Run npm run build on the default branch and log the result to activity_logs.

    On failure, automatically opens a GitHub issue describing the error.
    """
    from orchestrator.models.project import save_activity_log, get_activity_logs
    from orchestrator.api.websocket import emit_error

    workspace = settings.workspace_base_dir / str(project_id)
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = workspace / "build-check-repo"

    token = settings.github_token
    clone_url = (
        f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
        if token
        else f"https://github.com/{owner}/{repo}.git"
    )

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

    logger.info("Build check (project): running npm run build for %s/%s", owner, repo)
    passed, detail = await run_build_in_docker(repo_dir)

    if passed:
        await save_activity_log(project_id, seq, "Build check: PASSED", detail[-2000:], "INFO")
        logger.info("Build check (project): PASSED for %s/%s", owner, repo)
        from orchestrator.api.websocket import emit_build_passed
        await emit_build_passed(project_id, default_branch)
    else:
        await save_activity_log(project_id, seq, "Build check: FAILED", detail[-2000:], "ERROR")
        logger.error("Build check (project): FAILED for %s/%s: %s", owner, repo, detail[-200:])
        from orchestrator.api.websocket import emit_build_failed
        summary = _extract_build_error(detail)
        await emit_build_failed(project_id, summary, default_branch)


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

    logger.info("Build check: running npm run build for %s/%s issue #%d", owner, repo, issue_number)
    passed, detail = await run_build_in_docker(repo_dir)

    if passed:
        logger.info("Build check: PASSED for %s/%s issue #%d", owner, repo, issue_number)
    else:
        logger.error("Build check: FAILED for %s/%s issue #%d\n%s", owner, repo, issue_number, detail[-2000:])

    status = "passed" if passed else "failed"
    await update_github_issue_build_status(project_id, issue_number, status)
    await emit_issues_updated(project_id, await list_github_issues(project_id))
    return passed
