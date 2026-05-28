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
from orchestrator.logsafe import redact

logger = logging.getLogger(__name__)

# Per-project locks to prevent concurrent Aider runs from racing on the shared
# git workspace (one workspace per project, reused across issues). Without this,
# two simultaneous Implement clicks can `git checkout` over each other and lose
# uncommitted work.
_workspace_locks: dict[str, asyncio.Lock] = {}
_workspace_locks_guard = asyncio.Lock()


async def _get_workspace_lock(project_id: str) -> asyncio.Lock:
    async with _workspace_locks_guard:
        lock = _workspace_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            _workspace_locks[project_id] = lock
        return lock


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


async def _baseline_branch_builds(
    workspace: Path,
    authed_url: str,
    default_branch: str,
) -> bool:
    """Return True if the project's default branch builds clean.

    Used to decide whether to block a failing PR push. If main itself is broken,
    blocking PRs makes nothing land — so we only block when main is known-good.
    Result is uncached: builds are slow but safer than acting on stale state.
    """
    from orchestrator.build_check import run_build_in_docker
    baseline_dir = workspace / "baseline-check-repo"
    if (baseline_dir / ".git").exists():
        await _run(["git", "remote", "set-url", "origin", authed_url], cwd=baseline_dir)
        await _run(["git", "fetch", "origin", default_branch], cwd=baseline_dir, timeout=60)
        await _run(["git", "checkout", default_branch], cwd=baseline_dir)
        rc, _ = await _run(["git", "reset", "--hard", f"origin/{default_branch}"], cwd=baseline_dir)
    else:
        rc, _ = await _run(["git", "clone", "--branch", default_branch, authed_url, str(baseline_dir)], timeout=120)
    if rc != 0:
        logger.warning("Aider baseline: clone/sync failed — treating main as broken (push will be allowed)")
        return False
    passed, _ = await run_build_in_docker(baseline_dir)
    return passed


def _is_file_empty_or_unparseable(path: Path) -> bool:
    """Return True if the file is missing, near-empty, or has no recognizable code structure.

    Used to detect files that Aider created as placeholders but never filled. We don't
    full-parse — we just look for the keywords every real source file would have.
    """
    if not path.exists():
        return True
    try:
        size = path.stat().st_size
    except OSError:
        return True
    if size < 20:
        return True
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return True
    suffix = path.suffix.lower()
    if suffix == ".py":
        import ast
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return True
        # Empty module or only string literals / pass
        meaningful = [n for n in tree.body if not isinstance(n, (ast.Expr, ast.Pass))]
        return not meaningful
    if suffix in (".ts", ".tsx", ".js", ".jsx"):
        # Lightweight check — real source files always have at least one of these
        keywords = ("export", "import", "function", "const ", "let ", "class ", "interface ", "type ", "enum ")
        return not any(k in text for k in keywords)
    return False


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


async def _fill_missing_files(
    repo_dir: Path,
    missing: list[str],
    issue_body: str,
    model: str,
    env: dict[str, str],
) -> int:
    """Run a focused diff-mode Aider pass to create files the main run missed (token limit).

    Returns the number of files that now exist with real content.
    """
    import shutil

    for fp in missing:
        abs_fp = repo_dir / fp
        abs_fp.parent.mkdir(parents=True, exist_ok=True)
        if not abs_fp.exists():
            abs_fp.touch()

    missing_list = "\n".join(f"- {fp}" for fp in missing)
    task = (
        f"These files were required by the task but are still missing or empty. "
        f"Create each one now following the patterns described in the original task.\n\n"
        f"Missing files:\n{missing_list}\n\n"
        f"Original task context:\n{issue_body[:2000]}"
    )

    fill_args = [
        "--model", model,
        "--message", task,
        "--edit-format", "diff",
        "--yes", "--no-pretty", "--no-check-update", "--no-show-model-warnings",
        "--map-tokens", "4096", "--git", "--auto-commits",
    ]
    for fp in missing:
        fill_args.extend(["--file", fp])

    if shutil.which("aider"):
        cmd = ["aider", *fill_args]
    elif shutil.which("uvx"):
        cmd = ["uvx", "aider-chat", *fill_args]
    else:
        cmd = ["uv", "tool", "run", "aider-chat", *fill_args]

    rc, out = await _run_aider(cmd, cwd=repo_dir, env=env)
    logger.info("Aider fill-missing run (rc=%d):\n%s", rc, redact(out[-1000:]))

    return sum(
        1 for fp in missing
        if (repo_dir / fp).exists() and (repo_dir / fp).stat().st_size >= 20
    )


async def _implement_untouched_files(
    repo_dir: Path,
    untouched: list[str],
    issue_body: str,
    model: str,
    env: dict[str, str],
) -> int:
    """Aider promised to edit these files but didn't touch them. Run a focused diff-mode
    pass listing the missed files so it applies the same changes.

    Returns the number of files that now appear in `git status` (i.e. were actually edited).
    """
    import shutil

    untouched_list = "\n".join(f"- {fp}" for fp in untouched)
    task = (
        f"The original task required changes to these files, but the previous run did not "
        f"edit them. Apply the same pattern/changes you applied to similar files in the diff "
        f"so far. Each of these files needs the same treatment.\n\n"
        f"Files still needing changes:\n{untouched_list}\n\n"
        f"Original task context:\n{issue_body[:2000]}"
    )

    args = [
        "--model", model,
        "--message", task,
        "--edit-format", "diff",
        "--yes", "--no-pretty", "--no-check-update", "--no-show-model-warnings",
        "--map-tokens", "4096", "--git", "--auto-commits",
    ]
    for fp in untouched:
        args.extend(["--file", fp])

    if shutil.which("aider"):
        cmd = ["aider", *args]
    elif shutil.which("uvx"):
        cmd = ["uvx", "aider-chat", *args]
    else:
        cmd = ["uv", "tool", "run", "aider-chat", *args]

    rc, out = await _run_aider(cmd, cwd=repo_dir, env=env)
    logger.info("Aider implement-untouched run (rc=%d):\n%s", rc, redact(out[-1000:]))

    # Check which files now show up in `git diff` against HEAD~1 (after this follow-up commit)
    rc_diff, diff_out = await _run(
        ["git", "diff", "--name-only", "HEAD~1..HEAD"],
        cwd=repo_dir,
    )
    touched_now = set(diff_out.splitlines()) if rc_diff == 0 else set()
    return sum(1 for fp in untouched if fp in touched_now)


async def _enforce_aider_deletions(repo_dir: Path, default_branch: str) -> int:
    """Aider regularly writes commit messages claiming it deleted a file but never
    actually runs `git rm` — the file is still present (sometimes emptied, sometimes
    untouched). This breaks re-implement loops because the reviewer flags the same
    stray file every round.

    Parse this branch's commit messages for "delete <path>" / "remove <path>" intents
    and force-delete any matching files that still exist.
    Returns the number of files removed.
    """
    rc, log_out = await _run(
        ["git", "log", "--format=%B%n---END-COMMIT---", f"origin/{default_branch}..HEAD"],
        cwd=repo_dir,
    )
    if rc != 0 or not log_out.strip():
        return 0

    # Match: verb (delete/remove/drop/unlink/rm) followed by a backtick-quoted path,
    # OR a path-like token (must contain `/` or `.`) to reduce false positives.
    intent_re = re.compile(
        r"\b(?:delete[ds]?|remove[ds]?|drop[ped]?|unlink|rm)\b[^\n]{0,80}?"
        r"(?:`([^`\n]+)`|\"([^\"\n]+)\"|'([^'\n]+)'|([\w][\w./\- ]*[\w]))",
        re.IGNORECASE,
    )

    candidates: set[str] = set()
    for m in intent_re.finditer(log_out):
        raw = next((g for g in m.groups() if g), "").strip().strip("`'\"")
        if not raw or len(raw) > 200:
            continue
        # Must look like a file path: contains "/" or "." OR is a tab-suffixed garbage name
        if "/" not in raw and "." not in raw and "\t" not in raw:
            continue
        # Strip trailing punctuation that often grabs onto path tokens
        raw = raw.rstrip(".,;:)]}")
        # Must actually exist in the working tree, otherwise nothing to enforce
        if (repo_dir / raw).exists() and (repo_dir / raw).is_file():
            candidates.add(raw)

    if not candidates:
        return 0

    # Force delete via git rm
    paths_list = sorted(candidates)
    rc_rm, rm_out = await _run(
        ["git", "rm", "-f", "--", *paths_list],
        cwd=repo_dir,
    )
    if rc_rm != 0:
        logger.warning("Aider deletion enforcement: git rm failed: %s", rm_out[-300:])
        return 0

    await _run(
        ["git", "commit", "-m",
         f"chore: enforce {len(paths_list)} delete(s) Aider claimed but didn't perform\n\n"
         + "\n".join(f"- {p}" for p in paths_list)],
        cwd=repo_dir,
    )
    logger.warning(
        "Aider deletion enforcement: removed %d file(s) Aider claimed to delete: %s",
        len(paths_list), paths_list,
    )
    return len(paths_list)


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
    """Lock-wrapped entrypoint. Serialises all Aider runs for one project."""
    lock = await _get_workspace_lock(project_id)
    if lock.locked():
        logger.warning(
            "Aider: project %s already has an implementation in flight — queueing issue #%d",
            project_id, issue_number,
        )
    async with lock:
        return await _implement_issue_locked(
            project_id, owner, repo, issue_number, issue_title, issue_body,
            default_branch, model, is_reimplementation,
        )


async def _implement_issue_locked(
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

    # Remove untracked files left over from prior runs (e.g. empty placeholder
    # directories Aider created that were never committed). Respects .gitignore so
    # node_modules/.next survive. Without this, stale files sabotage subsequent builds.
    await _run(["git", "clean", "-fd"], cwd=repo_dir)

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
    logger.info("Aider output (rc=%d):\n%s", rc, redact(aider_out[-4000:]))

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
            safe_aider_out = redact(aider_out)
            logger.error("Aider: nothing to commit for issue #%d\nAider output:\n%s", issue_number, safe_aider_out)
            head = safe_aider_out[:1000].strip()
            tail = safe_aider_out[-500:].strip()
            snippet = f"{head}\n...\n{tail}" if len(safe_aider_out) > 1500 else safe_aider_out.strip() or "(no output)"
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
            safe_aider_out = redact(aider_out)
            head = safe_aider_out[:1000].strip()
            tail = safe_aider_out[-500:].strip()
            snippet = f"{head}\n...\n{tail}" if len(safe_aider_out) > 1500 else safe_aider_out.strip() or "(no output)"
            return None, f"Aider wrote no files (only housekeeping).\n\n{snippet}"

    # Correct any files that Aider wrote at paths with duplicated segments
    fixed = await _fix_duplicated_paths(repo_dir, default_branch)
    if fixed:
        logger.info("Aider: corrected %d duplicated-path file(s) for issue #%d", fixed, issue_number)

    # Move Next.js routing files that landed outside the app directory
    fixed_nextjs = await _fix_nextjs_page_paths(repo_dir, default_branch)
    if fixed_nextjs:
        logger.info("Aider: moved %d misplaced Next.js page file(s) for issue #%d", fixed_nextjs, issue_number)

    # Enforce file deletions Aider claimed in commit messages but didn't perform
    # (recurring failure mode — reviewer flags the stray file every re-implement round).
    enforced = await _enforce_aider_deletions(repo_dir, default_branch)
    if enforced:
        logger.info("Aider: enforced %d deletion(s) for issue #%d", enforced, issue_number)

    # Fill any files that Aider missed due to output-token limit (whole-mode truncation).
    # Two sources of "missing": (a) files declared in the issue body's file_paths, and
    # (b) files Aider touched in the commit but left empty / unparseable.
    if not is_reimplementation:
        candidate_files: set[str] = set()
        for fp in file_paths:
            candidate_files.add(fp)
        # Walk every file Aider committed on this branch
        for fp in meaningful_files:
            if fp.endswith((".ts", ".tsx", ".js", ".jsx", ".py")):
                candidate_files.add(fp)

        missing_files = [
            fp for fp in candidate_files
            if _is_file_empty_or_unparseable(repo_dir / fp)
        ]
        if missing_files:
            logger.warning(
                "Aider: %d file(s) empty or unparseable after main run for issue #%d — %s",
                len(missing_files), issue_number, missing_files,
            )
            filled = await _fill_missing_files(repo_dir, missing_files, issue_body, model, env)
            logger.info(
                "Aider: fill pass repaired %d/%d file(s) for issue #%d",
                filled, len(missing_files), issue_number,
            )

    # Completeness check: files the issue listed that Aider failed to touch at all.
    # Distinct from missing/empty — these files exist with content but Aider ignored them.
    # Common cause: multi-file refactor where Aider stops after 2-3 files due to output budget.
    if not is_reimplementation and file_paths:
        # Refresh meaningful_files list — fill-missing-files may have added new commits
        _, all_committed = await _run(
            ["git", "diff", "--name-only", f"origin/{default_branch}..HEAD"],
            cwd=repo_dir,
        )
        actually_touched = set(
            f for f in all_committed.splitlines()
            if f.strip() and f.strip() not in (".gitignore", ".aider.gitignore")
        )
        untouched_expected = [
            fp for fp in file_paths
            if fp not in actually_touched
            and (repo_dir / fp).exists()
            and not _is_file_empty_or_unparseable(repo_dir / fp)
        ]
        if untouched_expected:
            logger.warning(
                "Aider: %d expected file(s) not touched for issue #%d — %s",
                len(untouched_expected), issue_number, untouched_expected,
            )
            edited = await _implement_untouched_files(
                repo_dir, untouched_expected, issue_body, model, env,
            )
            logger.info(
                "Aider: implement-untouched pass edited %d/%d file(s) for issue #%d",
                edited, len(untouched_expected), issue_number,
            )

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
        logger.info("Aider fix run output:\n%s", redact(fix_out[-2000:]))

        build_passed2, build_output2 = await run_build_in_docker(repo_dir)
        if not build_passed2:
            # Only block the push when main is known to build. If main itself is broken,
            # blocking would prevent any fix-the-build PRs from landing.
            main_builds = await _baseline_branch_builds(workspace, authed_url, default_branch)
            from orchestrator.build_check import _extract_build_error as _xerr
            summary = redact(_xerr(build_output2))
            # Always log the full build error to stdout so it shows in `docker logs`
            logger.error(
                "Aider: pre-push build error for issue #%d:\n%s",
                issue_number, summary[-3000:],
            )
            # Persist to activity_logs so it's visible in the UI Activity tab
            try:
                from orchestrator.models.project import save_activity_log, get_activity_logs
                existing = await get_activity_logs(project_id)
                seq = (max((r.get("seq", 0) for r in existing), default=0) + 1)
                await save_activity_log(
                    project_id, seq,
                    f"Aider build failed for issue #{issue_number}",
                    summary[-3000:],
                    "ERROR",
                )
            except Exception as exc:
                logger.warning("Aider: failed to persist build error to activity log: %s", exc)
            if main_builds:
                logger.error(
                    "Aider: blocking push — main builds clean, this PR doesn't (issue #%d)",
                    issue_number,
                )
                from orchestrator.api.websocket import emit_aider_needs_help
                await emit_aider_needs_help(project_id, issue_number, summary)
                return None, f"Pre-push build failed and main is clean — Aider needs human help.\n\n{summary[-2000:]}"
            logger.warning(
                "Aider: build still failing after retry for issue #%d — main is also broken, pushing for review",
                issue_number,
            )
        else:
            logger.info("Aider: build passed after retry for issue #%d", issue_number)
    else:
        logger.info("Aider: pre-push build PASSED for issue #%d", issue_number)

    # Remove any accidentally committed large files / node_modules before pushing
    await _run(["git", "rm", "-r", "--cached", "--ignore-unmatch",
                "node_modules", ".next", ".nuxt", "dist", "__pycache__",
                "*.node", "*.pyc"], cwd=repo_dir)
    # Re-check if that produced a diff worth committing
    rc_status, status_out = await _run(["git", "status", "--porcelain"], cwd=repo_dir)
    if status_out.strip():
        await _run(["git", "commit", "-m", "chore: remove large/generated files from tracking"], cwd=repo_dir)

    # Push branch
    await _run(["git", "remote", "set-url", "origin", authed_url], cwd=repo_dir)
    rc, push_out = await _run(
        ["git", "push", "-u", "origin", branch, "--force-with-lease"],
        cwd=repo_dir,
        timeout=60,
    )
    if rc != 0:
        safe_push_out = redact(push_out)
        logger.error("Aider: push failed:\n%s", safe_push_out)
        return None, f"git push failed: {safe_push_out[-500:]}"

    logger.info("Aider: pushed branch %s for issue #%d (%s mode)", branch, issue_number, edit_format)
    return branch, None
