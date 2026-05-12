"""Aider-based autonomous code implementation agent."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from orchestrator.config import settings

logger = logging.getLogger(__name__)


async def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode(errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        return 1, f"Command timed out after {timeout}s"


async def implement_issue(
    project_id: str,
    owner: str,
    repo: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    default_branch: str = "main",
    model: str = "claude-opus-4-5",
) -> str | None:
    """
    Clone/pull the repo, run Aider on the issue, push a branch.
    Returns the branch name on success, None on failure.
    """
    workspace = settings.workspace_base_dir / str(project_id)
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = workspace / "repo"

    token = settings.github_token
    authed_url = (
        f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
        if token
        else f"https://github.com/{owner}/{repo}.git"
    )

    # Clone or sync to latest default branch
    if (repo_dir / ".git").exists():
        logger.info("Aider: syncing %s/%s", owner, repo)
        await _run(["git", "remote", "set-url", "origin", authed_url], cwd=repo_dir)
        await _run(["git", "fetch", "origin"], cwd=repo_dir, timeout=60)
        await _run(["git", "checkout", default_branch], cwd=repo_dir)
        await _run(["git", "reset", "--hard", f"origin/{default_branch}"], cwd=repo_dir)
    else:
        logger.info("Aider: cloning %s/%s", owner, repo)
        rc, out = await _run(["git", "clone", authed_url, str(repo_dir)], timeout=120)
        if rc != 0:
            logger.error("Aider: clone failed:\n%s", out)
            return None

    branch = f"aider/fix-{issue_number}"
    await _run(["git", "checkout", "-B", branch], cwd=repo_dir)

    # Build the task prompt
    task = f"# Task: {issue_title}\n\n{issue_body}\n\nImplement the task described above. Follow all implementation notes and acceptance criteria exactly."

    # Inherit env and inject API key
    env = {**os.environ}
    if settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    if settings.github_token:
        env["GITHUB_TOKEN"] = settings.github_token

    import shutil

    aider_args = [
        "--model", f"claude/{model}",
        "--message", task,
        "--yes",
        "--no-pretty",
        "--no-check-update",
        "--no-show-model-warnings",
        "--git",
        "--auto-commits",
    ]

    if shutil.which("aider"):
        aider_cmd = ["aider", *aider_args]
    elif shutil.which("uvx"):
        aider_cmd = ["uvx", "aider-chat", *aider_args]
    elif shutil.which("uv"):
        aider_cmd = ["uv", "tool", "run", "aider-chat", *aider_args]
    else:
        logger.error("Aider: no aider/uvx/uv binary found in PATH")
        return None

    logger.info("Aider: running on issue #%d (%s)", issue_number, issue_title)
    rc, out = await _run(aider_cmd, cwd=repo_dir, env=env, timeout=600)
    logger.info("Aider output (rc=%d):\n%s", rc, out[-3000:])

    # Check something was committed
    rc_log, log_out = await _run(
        ["git", "log", f"origin/{default_branch}..HEAD", "--oneline"],
        cwd=repo_dir,
    )
    if not log_out.strip():
        logger.warning("Aider: no commits made for issue #%d", issue_number)
        # Still push in case aider made changes but didn't commit — force commit
        await _run(["git", "add", "-A"], cwd=repo_dir)
        rc_commit, _ = await _run(
            ["git", "commit", "-m", f"fix: implement #{issue_number} {issue_title}"],
            cwd=repo_dir,
        )
        if rc_commit != 0:
            logger.error("Aider: nothing to commit for issue #%d", issue_number)
            return None

    # Push branch
    await _run(["git", "remote", "set-url", "origin", authed_url], cwd=repo_dir)
    rc, out = await _run(
        ["git", "push", "-u", "origin", branch, "--force-with-lease"],
        cwd=repo_dir,
        timeout=60,
    )
    if rc != 0:
        logger.error("Aider: push failed:\n%s", out)
        return None

    logger.info("Aider: pushed branch %s for issue #%d", branch, issue_number)
    return branch
