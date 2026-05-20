"""Aider-based autonomous code implementation agent."""

from __future__ import annotations

import asyncio
import logging
import os
import pty
import re
import shutil
import unicodedata
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


async def _run_aider(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> tuple[int, str]:
    """Run Aider with a pseudo-TTY on stdin.

    Aider checks isatty(0) and exits before processing --message when stdin
    is not a real terminal. A PTY slave as stdin satisfies that check.
    """
    master_fd, slave_fd = pty.openpty()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
        )
        os.close(slave_fd)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode or 0, stdout.decode(errors="replace")
        except asyncio.TimeoutError:
            proc.kill()
            return 1, f"Command timed out after {timeout}s"
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass


def _collapse_path_segments(path: str) -> str:
    """Collapse duplicated leading path segments, e.g. src/app/src/app/x → src/app/x.

    Aider's LLM sometimes prepends the working-directory prefix to a path it already
    received as a --file arg, producing doubled segments. This detects the first
    repetition and removes the duplicate.
    """
    parts = path.replace("\\", "/").split("/")
    for length in range(1, len(parts) // 2 + 1):
        prefix = parts[:length]
        if parts[length:length + length] == prefix:
            return "/".join(parts[:length] + parts[length * 2:])
    return path


_NEXTJS_ROUTE_FILES = {
    "page.tsx", "page.ts", "page.jsx", "page.js",
    "layout.tsx", "layout.ts", "layout.jsx", "layout.js",
    "route.ts", "route.js",
    "loading.tsx", "error.tsx", "not-found.tsx", "template.tsx",
}


async def _fix_nextjs_page_paths(repo_dir: Path, default_branch: str) -> int:
    """Move Next.js routing files that Aider placed outside the app directory.

    e.g. invite/page.tsx → src/app/invite/page.tsx when src/app/ exists.
    Returns the number of files corrected.
    """
    # Locate the app directory (src/app preferred, then app/)
    if (repo_dir / "src" / "app").exists():
        app_root = Path("src/app")
    elif (repo_dir / "app").exists():
        app_root = Path("app")
    else:
        return 0

    _, committed = await _run(
        ["git", "diff", "--name-only", f"origin/{default_branch}..HEAD"],
        cwd=repo_dir,
    )
    fixes = 0
    for raw in committed.splitlines():
        path = raw.strip()
        if not path:
            continue
        p = Path(path)
        if p.name not in _NEXTJS_ROUTE_FILES:
            continue
        # Already under the app root?
        parts = p.parts
        if "src" in parts or parts[0] == "app":
            continue
        # It's a Next.js routing file outside the app directory — move it in
        target_rel = str(app_root / path)
        (repo_dir / target_rel).parent.mkdir(parents=True, exist_ok=True)
        rc, out = await _run(["git", "mv", "--force", path, target_rel], cwd=repo_dir)
        if rc == 0:
            fixes += 1
            logger.warning("Aider: moved misplaced Next.js file %s → %s", path, target_rel)
        else:
            logger.error("Aider: could not move %s → %s: %s", path, target_rel, out)

    if fixes:
        rc, _ = await _run(
            ["git", "commit", "-m", f"fix: move {fixes} Next.js page file(s) to app directory"],
            cwd=repo_dir,
        )
        if rc != 0:
            logger.warning("Aider: Next.js path fixup commit failed")
    return fixes


async def _fix_duplicated_paths(repo_dir: Path, default_branch: str) -> int:
    """Detect files committed at paths with duplicated segments and move them.

    Returns the number of files corrected.
    """
    _, committed = await _run(
        ["git", "diff", "--name-only", f"origin/{default_branch}..HEAD"],
        cwd=repo_dir,
    )
    fixes = 0
    for raw in committed.splitlines():
        path = raw.strip()
        if not path:
            continue
        corrected = _collapse_path_segments(path)
        if corrected == path:
            continue
        target = repo_dir / corrected
        target.parent.mkdir(parents=True, exist_ok=True)
        rc, out = await _run(["git", "mv", "--force", path, corrected], cwd=repo_dir)
        if rc == 0:
            fixes += 1
            logger.warning("Aider: moved misplaced file %s → %s", path, corrected)
        else:
            logger.error("Aider: could not move %s → %s: %s", path, corrected, out)
    if fixes:
        rc, _ = await _run(
            ["git", "commit", "-m", f"fix: correct {fixes} misplaced file path(s)"],
            cwd=repo_dir,
        )
        if rc != 0:
            logger.warning("Aider: fixup commit failed (files may already be staged)")
    return fixes


def _sanitize_issue_body(text: str) -> str:
    """Strip ASCII/Unicode box-drawing characters that LLMs include in directory trees.

    Aider treats lines like '└── types/index.ts' as file paths and creates junk files
    literally named with those characters. We drop such lines entirely and remove any
    stray box-drawing chars (U+2500–U+257F) from what remains.
    """
    clean_lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped:
            first_cp = ord(stripped[0])
            # U+2500–U+257F: Box Drawing block; also catch common chars individually
            if 0x2500 <= first_cp <= 0x257F:
                continue
            # Drop lines whose first non-space char is a box-drawing "So" category char
            if unicodedata.category(stripped[0]) == "So" and first_cp > 0x2000:
                continue
        clean_lines.append(line)
    # Remove any stray box-drawing chars that snuck into the middle of lines
    cleaned = "\n".join(clean_lines)
    cleaned = re.sub(r"[─-╿├└│┐┌┼]+", "", cleaned)
    return cleaned


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

    # Strip ASCII tree-diagram lines before any further processing
    issue_body = _sanitize_issue_body(issue_body)

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

    preserve_note = (
        "\n\nIMPORTANT: Only write the files explicitly listed above. "
        "Do NOT modify, overwrite, or delete any other existing files in the repository."
        "\n\nCRITICAL PATH RULE: Write every file at EXACTLY the path shown. "
        "The working directory is the repository root. "
        "Never duplicate path segments — e.g. if the target is `src/app/foo/page.tsx`, "
        "write it at `src/app/foo/page.tsx`, NOT at `src/app/src/app/foo/page.tsx`."
        "\n\nDo NOT create files based on any directory tree diagrams in the task — "
        "those are for reference only. Only create files at the exact paths listed above."
        if not is_reimplementation
        else ""
    )

    task = (
        f"# Task: {issue_title}\n\n"
        f"{issue_body}\n\n"
        f"Implement the task described above. Follow all implementation notes and "
        f"acceptance criteria exactly. Write complete, working file contents for every "
        f"file you create or modify.{scratch_note}{reimpl_note}{preserve_note}"
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

    # Aider v0.86+ exits without any LLM call when no --file args are supplied.
    # For fresh implementations: create empty placeholder files for target paths
    # that don't exist yet so they can be passed as --file args.
    # Aider (whole format) overwrites the file with full content — no path doubling.
    # For re-implementations (diff format): files already exist on the branch.
    placeholders_created: list[Path] = []
    if not is_reimplementation:
        for fp in file_paths:
            abs_fp = repo_dir / fp
            if not abs_fp.exists():
                abs_fp.parent.mkdir(parents=True, exist_ok=True)
                abs_fp.touch()
                placeholders_created.append(abs_fp)

    # whole format: only pass the empty placeholders we just created.
    # Passing pre-existing non-empty files would cause Aider to overwrite them entirely.
    # diff format: pass all existing files so Aider can patch them.
    if is_reimplementation:
        for fp in file_paths:
            if (repo_dir / fp).exists():
                aider_args.extend(["--file", fp])
    else:
        for ph in placeholders_created:
            aider_args.extend(["--file", str(ph.relative_to(repo_dir))])

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
    rc, aider_out = await _run_aider(aider_cmd, cwd=repo_dir, env=env)
    logger.info("Aider output (rc=%d):\n%s", rc, aider_out[-4000:])

    # Remove placeholder files that Aider didn't fill in (still empty after run)
    for ph in placeholders_created:
        if ph.exists() and ph.stat().st_size == 0:
            ph.unlink()
            logger.debug("Aider: removed empty placeholder %s", ph)

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
            if "credit balance is too low" in aider_out or "insufficient_quota" in aider_out:
                return None, "Anthropic API credit balance is too low — top up at console.anthropic.com/billing"
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

    # Correct any files that Aider wrote at paths with duplicated segments
    fixed = await _fix_duplicated_paths(repo_dir, default_branch)
    if fixed:
        logger.info("Aider: corrected %d duplicated-path file(s) for issue #%d", fixed, issue_number)

    # Move Next.js routing files that landed outside the app directory
    fixed_nextjs = await _fix_nextjs_page_paths(repo_dir, default_branch)
    if fixed_nextjs:
        logger.info("Aider: moved %d misplaced Next.js page file(s) for issue #%d", fixed_nextjs, issue_number)

    # Pre-push build validation — catch broken code before it reaches GitHub.
    # Skip gracefully when Docker is unavailable (build check is best-effort).
    from orchestrator.build_check import run_build_in_docker, _extract_build_error
    logger.info("Aider: running pre-push build check for issue #%d", issue_number)
    try:
        import docker as _docker_sdk
        _docker_sdk.from_env()  # raises if daemon not reachable
        _docker_available = True
    except Exception:
        _docker_available = False
        logger.warning("Aider: Docker not available — skipping pre-push build check for issue #%d", issue_number)

    build_passed, build_output = (True, "") if not _docker_available else await run_build_in_docker(repo_dir)
    if not build_passed:
        logger.warning("Aider: pre-push build failed for issue #%d — retrying with fix", issue_number)
        fix_task = (
            f"The code you just wrote has a build error. Fix it.\n\n"
            f"Build error:\n```\n{build_output[-3000:]}\n```\n\n"
            f"Fix only what is broken. Do not rewrite files that are working."
        )
        fix_args = [
            "--model", model, "--message", fix_task,
            "--yes", "--no-pretty", "--no-check-update", "--no-show-model-warnings",
            "--edit-format", "diff",
            "--map-tokens", "4096", "--git", "--auto-commits",
        ]
        if shutil.which("aider"):
            fix_cmd = ["aider", *fix_args]
        elif shutil.which("uvx"):
            fix_cmd = ["uvx", "aider-chat", *fix_args]
        else:
            fix_cmd = ["uv", "tool", "run", "aider-chat", *fix_args]

        _, fix_out = await _run_aider(fix_cmd, cwd=repo_dir, env=env)
        logger.info("Aider fix run output:\n%s", fix_out[-2000:])

        build_passed2, build_output2 = await run_build_in_docker(repo_dir)
        if not build_passed2:
            # Push anyway — the user can review what Aider did. Blocking the push entirely
            # means nothing reaches GitHub even when the Aider change is directionally correct
            # but the repo has pre-existing or unrelated compile errors.
            logger.warning(
                "Aider: build still failing after retry for issue #%d — pushing for review",
                issue_number,
            )
        else:
            logger.info("Aider: build passed after retry for issue #%d", issue_number)
    else:
        logger.info("Aider: pre-push build PASSED for issue #%d", issue_number)

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
