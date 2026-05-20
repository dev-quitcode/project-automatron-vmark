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
        stdin=asyncio.subprocess.DEVNULL,
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
    """Pull file paths out of an issue body.

    Supports:
    - Route groups: src/app/(authenticated)/settings/page.tsx
    - Dynamic segments: src/app/api/organizations/[id]/users/route.ts
    Only returns paths that contain at least one '/' — bare filenames like
    'page.tsx' are skipped to avoid creating stray root-level files.
    """
    # Segment: any dir name including (group) and [param] syntax
    _seg = r'[A-Za-z0-9_\-\.\(\)\[\]]+'
    _ext = r'[A-Za-z0-9]{1,10}'

    # Backtick-quoted paths that contain at least one slash
    backtick = re.findall(rf'`((?:{_seg}/)+{_seg}\.{_ext})`', text)

    # Bare paths starting with a known root directory
    root_files = re.findall(
        rf'\b((?:src|app|lib|components|pages|hooks|utils|types|styles|config|public|supabase)'
        rf'(?:/{_seg})+\.{_ext})\b',
        text,
    )

    # Well-known root config files (no slash needed — these are real root files)
    _ROOT_CONFIGS = {
        "package.json", "tsconfig.json", ".env.local", ".env",
        "tailwind.config.ts", "tailwind.config.js",
        "next.config.ts", "next.config.js", "next.config.mjs",
        "postcss.config.js", "postcss.config.mjs",
        "eslint.config.js", "eslint.config.mjs",
    }
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
        if not p or len(p) >= 120:
            continue
        # Skip bare filenames that aren't known root configs
        if "/" not in p and p not in _ROOT_CONFIGS:
            continue
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result[:30]


async def implement_issue(
    project_id: str,
    owner: str,
    repo: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    default_branch: str = "main",
    model: str = "anthropic/claude-sonnet-4-6",
    is_reimplementation: bool = False,
) -> tuple[str | None, str | None]:
    """
    Clone/pull the repo, run Aider on the issue, push a branch.

    For re-implementation (is_reimplementation=True), continues from the existing
    aider/fix-{issue_number} branch using diff format to patch rather than rewrite.
    Returns (branch_name, None) on success, (None, failure_reason) on failure.
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

    branch = f"aider/fix-{issue_number}"

    # Clone or sync repo
    if (repo_dir / ".git").exists():
        logger.info("Aider: syncing %s/%s", owner, repo)
        await _run(["git", "remote", "set-url", "origin", authed_url], cwd=repo_dir)
        await _run(["git", "fetch", "origin"], cwd=repo_dir, timeout=60)
    else:
        logger.info("Aider: cloning %s/%s", owner, repo)
        rc, out = await _run(["git", "clone", authed_url, str(repo_dir)], timeout=120)
        if rc != 0:
            logger.error("Aider: clone failed:\n%s", out)
            return None, f"git clone failed: {out[-500:]}"
        await _run(["git", "fetch", "origin"], cwd=repo_dir, timeout=60)

    # For re-implementation: continue from the existing branch so files that were
    # implemented correctly are still present. Aider patches only what the review flagged.
    # For fresh implementation: reset to default_branch to avoid stale wrong files.
    _, remote_branches = await _run(["git", "ls-remote", "--heads", "origin", branch], cwd=repo_dir)
    branch_on_remote = branch in remote_branches

    if is_reimplementation and branch_on_remote:
        logger.info("Aider: continuing from existing branch %s for issue #%d", branch, issue_number)
        await _run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir)
    else:
        logger.info("Aider: resetting to %s for issue #%d", default_branch, issue_number)
        await _run(["git", "checkout", default_branch], cwd=repo_dir)
        await _run(["git", "reset", "--hard", f"origin/{default_branch}"], cwd=repo_dir)
        await _run(["git", "checkout", "-B", branch], cwd=repo_dir)

    # Re-implementation: diff patches only the broken parts in existing files on the branch.
    # Fresh implementation: whole writes complete new files from scratch (diff needs files
    # already in context, which is impossible when the target files don't exist yet).
    edit_format = "diff" if is_reimplementation else "whole"

    # Detect empty/minimal repo
    non_git_files = [
        f for f in repo_dir.rglob("*")
        if ".git" not in f.parts and f.is_file() and f.name != ".aider.gitignore"
    ]
    is_empty_repo = len(non_git_files) <= 2

    # Extract file paths from the issue so Aider has explicit targets
    file_paths = _extract_file_paths(issue_body)
    logger.info("Aider: extracted %d file targets: %s", len(file_paths), file_paths)

    scratch_note = (
        "\n\nIMPORTANT: This repository is new and mostly empty. "
        "You MUST CREATE all necessary files from scratch. "
        "Do not assume any framework files exist. "
        "Write the complete content of every file listed above."
        if is_empty_repo and not is_reimplementation
        else ""
    )

    reimpl_note = (
        "\n\nIMPORTANT: A previous attempt was reviewed and had the errors listed above. "
        "The existing files on this branch are the previous attempt. "
        "Fix ONLY the issues flagged in the review — do not rewrite files that were marked as correct."
        if is_reimplementation and branch_on_remote
        else (
            "\n\nIMPORTANT: A previous attempt had errors described in the review feedback above. "
            "Write all required files from scratch at the exact paths specified."
            if is_reimplementation
            else ""
        )
    )

    task = (
        f"# Task: {issue_title}\n\n"
        f"{issue_body}\n\n"
        f"Implement the task described above. Follow all implementation notes and "
        f"acceptance criteria exactly. Write complete, working file contents for every "
        f"file you create or modify.{scratch_note}{reimpl_note}"
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
        "--edit-format", edit_format,
        "--map-tokens", "4096",
        "--git",
        "--auto-commits",
    ]

    # Only pass --file for files that ALREADY EXIST in the repo.
    # For new files, let Aider create them via --- /dev/null diff convention.
    # Pre-creating empty placeholders confuses the LLM into doubling path prefixes.
    for fp in file_paths:
        if (repo_dir / fp).exists():
            aider_args.extend(["--file", fp])

    if shutil.which("aider"):
        aider_cmd = ["aider", *aider_args]
    elif shutil.which("uvx"):
        aider_cmd = ["uvx", "aider-chat", *aider_args]
    elif shutil.which("uv"):
        aider_cmd = ["uv", "tool", "run", "aider-chat", *aider_args]
    else:
        logger.error("Aider: no aider/uvx/uv binary found in PATH")
        return None, "aider binary not found in PATH"

    logger.info("Aider: running %s on issue #%d (%s)", edit_format, issue_number, issue_title)
    rc, aider_out = await _run(aider_cmd, cwd=repo_dir, env=env, timeout=600)
    logger.info("Aider output (rc=%d):\n%s", rc, aider_out[-4000:])

    # Check something meaningful was committed (not just .gitignore housekeeping)
    _, committed_files = await _run(
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
            logger.error("Aider: nothing to commit for issue #%d\nAider output:\n%s", issue_number, aider_out)
            head = aider_out[:1000].strip()
            tail = aider_out[-500:].strip()
            snippet = f"{head}\n...\n{tail}" if len(aider_out) > 1500 else aider_out.strip() or "(no output)"
            return None, f"Aider made no changes (rc={rc}).\n\n{snippet}"
        # Re-check after force commit
        _, committed_files2 = await _run(
            ["git", "diff", "--name-only", f"origin/{default_branch}..HEAD"],
            cwd=repo_dir,
        )
        meaningful_files = [
            f for f in committed_files2.splitlines()
            if f.strip() and f.strip() not in (".gitignore", ".aider.gitignore")
        ]
        if not meaningful_files:
            logger.error("Aider: still no meaningful files for issue #%d", issue_number)
            head = aider_out[:1000].strip()
            tail = aider_out[-500:].strip()
            snippet = f"{head}\n...\n{tail}" if len(aider_out) > 1500 else aider_out.strip() or "(no output)"
            return None, f"Aider wrote no files (only housekeeping).\n\n{snippet}"

    # Push branch
    await _run(["git", "remote", "set-url", "origin", authed_url], cwd=repo_dir)
    rc, push_out = await _run(
        ["git", "push", "-u", "origin", branch, "--force-with-lease"],
        cwd=repo_dir,
        timeout=60,
    )
    if rc != 0:
        logger.error("Aider: push failed:\n%s", push_out)
        return None, f"git push failed: {push_out[-500:]}"

    logger.info("Aider: pushed branch %s for issue #%d (%s mode)", branch, issue_number, edit_format)
    return branch, None
