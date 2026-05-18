"""Aider-based autonomous code implementation agent."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
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


def _extract_file_paths(text: str) -> list[str]:
    """Pull file paths out of an issue body (backtick-quoted or plain path patterns)."""
    # Match paths in backticks: `src/lib/foo.ts`
    backtick = re.findall(r'`([A-Za-z_\-\.][A-Za-z0-9_\-\./]*\.[A-Za-z0-9]{1,10})`', text)
    # Match bare paths like src/lib/foo.ts (must contain / or be a known root file)
    root_files = re.findall(
        r'\b((?:src|app|lib|components|pages|hooks|utils|types|styles|config|public)'
        r'(?:/[A-Za-z0-9_\-\.]+)+\.[A-Za-z0-9]{1,10})\b',
        text,
    )
    # Well-known root config files
    config_files = re.findall(
        r'\b((?:package|tsconfig|tailwind\.config|next\.config|postcss\.config|'
        r'\.env(?:\.local)?|eslint\.config|prettier\.config|vite\.config|'
        r'vitest\.config|jest\.config)'
        r'(?:\.[a-z]{1,5})?)\b',
        text,
    )
    seen: set[str] = set()
    result: list[str] = []
    for p in (*backtick, *root_files, *config_files):
        p = p.strip()
        if p and p not in seen and len(p) < 120:
            seen.add(p)
            result.append(p)
    return result[:30]  # cap at 30 files


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

    # Detect empty/minimal repo
    non_git_files = [
        f for f in repo_dir.rglob("*")
        if ".git" not in f.parts and f.is_file() and f.name != ".aider.gitignore"
    ]
    is_empty_repo = len(non_git_files) <= 2

    # Extract file paths from the issue so Aider has explicit targets
    file_paths = _extract_file_paths(issue_body)
    logger.info("Aider: extracted %d file targets from issue body: %s", len(file_paths), file_paths)

    scratch_note = (
        "\n\nIMPORTANT: This repository is new and mostly empty. "
        "You MUST CREATE all necessary files from scratch. "
        "Do not assume any framework files exist. "
        "Write the complete content of every file listed above."
        if is_empty_repo
        else ""
    )

    task = (
        f"# Task: {issue_title}\n\n"
        f"{issue_body}\n\n"
        f"Implement the task described above. Follow all implementation notes and "
        f"acceptance criteria exactly. Write complete, working file contents for every "
        f"file you create or modify.{scratch_note}"
    )

    env = {**os.environ}
    if settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    if settings.github_token:
        env["GITHUB_TOKEN"] = settings.github_token

    aider_args = [
        "--model", model,
        "--message", task,
        "--yes",
        "--no-pretty",
        "--no-check-update",
        "--no-show-model-warnings",
        "--edit-format", "whole",
        "--git",
        "--auto-commits",
    ]

    # Pass extracted file paths so Aider creates/edits them directly
    for fp in file_paths:
        aider_args.extend(["--file", fp])

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
    logger.info("Aider output (rc=%d):\n%s", rc, out[-4000:])

    # Check something meaningful was committed (not just .gitignore housekeeping)
    rc_log, log_out = await _run(
        ["git", "log", f"origin/{default_branch}..HEAD", "--oneline"],
        cwd=repo_dir,
    )
    committed_files_rc, committed_files = await _run(
        ["git", "diff", "--name-only", f"origin/{default_branch}..HEAD"],
        cwd=repo_dir,
    )
    meaningful_files = [
        f for f in committed_files.splitlines()
        if f.strip() and f.strip() not in (".gitignore", ".aider.gitignore")
    ]

    if not meaningful_files:
        logger.warning("Aider: no meaningful files committed for issue #%d — forcing add", issue_number)
        await _run(["git", "add", "-A"], cwd=repo_dir)
        rc_commit, commit_out = await _run(
            ["git", "commit", "-m", f"fix: implement #{issue_number} {issue_title}"],
            cwd=repo_dir,
        )
        logger.info("Force commit rc=%d: %s", rc_commit, commit_out[:500])
        if rc_commit != 0:
            logger.error("Aider: nothing to commit for issue #%d\nAider output:\n%s", issue_number, out[-2000:])
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
