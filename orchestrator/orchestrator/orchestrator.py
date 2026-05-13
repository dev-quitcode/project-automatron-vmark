"""GitHub-native orchestrator — replaces LangGraph-based graph execution."""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from orchestrator.api.websocket import (
    emit_architect_chunk,
    emit_architect_message,
    emit_builder_log,
    emit_error,
    emit_human_required,
    emit_issues_updated,
    emit_pr_review_ready,
    emit_status_update,
)
from orchestrator.config import settings
from orchestrator.github.issues import GitHubClient
from orchestrator.llm.configuration import normalize_llm_config
from orchestrator.llm.provider import call_llm, call_llm_streaming
from orchestrator.models.project import (
    create_github_issue,
    get_project,
    list_github_issues,
    record_approval,
    save_activity_log,
    update_github_issue_pr,
    update_github_issue_status,
    update_project,
    update_project_preview,
)
from orchestrator.observability import trace_event

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent.parent / "prompts"
_DEPLOY_AUDIT_ISSUE_TITLE = "chore: deploy readiness audit (Automatron)"
_DEPLOY_AUDIT_LABEL = "deploy-audit"


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _parse_tagged_block(response: str, tag: str) -> str | None:
    marker = f"```{tag}"
    if marker not in response:
        return None
    try:
        start = response.index(marker) + len(marker)
        end = response.index("```", start)
        return response[start:end].strip() or None
    except ValueError:
        return None


def _parse_repo_url(repo_url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub URL or owner/repo string."""
    # https://github.com/owner/repo  or  github.com/owner/repo
    match = re.search(r"github\.com/([^/]+)/([^/?\s#]+)", repo_url)
    if match:
        return match.group(1), match.group(2).rstrip(".git")
    # owner/repo shorthand
    parts = repo_url.strip().split("/")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1].rstrip(".git")
    return None


def _read_source_files(repo_dir: "Path", max_chars: int = 40000) -> str:
    """Walk repo_dir and return key source files as a formatted string."""
    import os as _os

    SKIP_DIRS = {
        "node_modules", ".git", ".next", "dist", "build", ".venv",
        "__pycache__", ".cache", "coverage", ".turbo", "out", ".mypy_cache",
    }
    INCLUDE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".py", ".css", ".md"}
    SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}

    parts: list[str] = []
    total = 0

    for dirpath, dirnames, filenames in _os.walk(repo_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in sorted(filenames):
            if filename in SKIP_FILES:
                continue
            path = Path(dirpath) / filename
            if path.suffix not in INCLUDE_EXTS:
                continue
            rel = path.relative_to(repo_dir)
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                if len(content) > 5000:
                    content = content[:5000] + "\n... [truncated]"
                entry = f"### {rel}\n```\n{content}\n```\n\n"
                parts.append(entry)
                total += len(entry)
                if total >= max_chars:
                    return "\n".join(parts)
            except Exception:
                pass

    return "\n".join(parts)


async def _fetch_figma_context(urls: list[str], token: str) -> str:
    """Fetch frame/component names and text from Figma files and return as text summary."""
    import httpx

    parts: list[str] = []
    async with httpx.AsyncClient(timeout=15) as client:
        for url in urls:
            file_key_m = re.search(r"figma\.com/(?:design|file)/([^/?#]+)", url)
            if not file_key_m:
                parts.append(f"[Skipped — not a valid Figma URL: {url}]")
                continue
            file_key = file_key_m.group(1)
            node_id_m = re.search(r"node-id=([^&]+)", url)

            try:
                if node_id_m:
                    node_id = node_id_m.group(1).replace("-", ":")
                    resp = await client.get(
                        f"https://api.figma.com/v1/files/{file_key}/nodes",
                        params={"ids": node_id},
                        headers={"X-Figma-Token": token},
                    )
                else:
                    resp = await client.get(
                        f"https://api.figma.com/v1/files/{file_key}",
                        headers={"X-Figma-Token": token},
                    )
            except Exception as exc:
                parts.append(f"[Figma fetch error for {url}: {exc}]")
                continue

            if resp.status_code != 200:
                parts.append(f"[Figma {url}: HTTP {resp.status_code}]")
                continue

            summary = _summarise_figma_node(resp.json())
            parts.append(f"**{url}**\n{summary}")

    return "\n\n".join(parts)


def _summarise_figma_node(data: dict[str, Any]) -> str:
    """Walk Figma JSON and extract frame/component names and visible text (max 100 lines)."""
    lines: list[str] = []

    def walk(node: dict[str, Any], depth: int = 0) -> None:
        if len(lines) >= 100 or depth > 5:
            return
        node_type = node.get("type", "")
        name = node.get("name", "")
        chars = node.get("characters", "")
        indent = "  " * depth
        if node_type in ("FRAME", "COMPONENT", "COMPONENT_SET", "SECTION", "GROUP", "PAGE"):
            lines.append(f"{indent}- [{node_type}] {name}")
        elif chars:
            lines.append(f'{indent}  "{chars[:80]}"')
        for child in node.get("children", []):
            walk(child, depth + 1)

    doc = data.get("document") or {}
    nodes = data.get("nodes") or {}
    if doc:
        walk(doc)
    for node_data in nodes.values():
        walk(node_data.get("document", node_data))

    return "\n".join(lines)


def _render_issue_body(task: dict[str, Any], epic: str, story: str) -> str:
    lines: list[str] = []
    lines.append(f"**Epic:** {epic}  ")
    lines.append(f"**Story:** {story}")
    lines.append("")

    if task.get("description"):
        lines.append("## Overview")
        lines.append(task["description"])
        lines.append("")

    if task.get("file") or task.get("component"):
        lines.append("## Scope")
        if task.get("file"):
            lines.append(f"- **Primary file:** `{task['file']}`")
        if task.get("component"):
            lines.append(f"- **Component/function:** `{task['component']}`")
        lines.append("")

    notes = task.get("implementation_notes") or []
    if notes:
        lines.append("## Implementation notes")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")

    criteria = task.get("acceptance_criteria") or []
    if criteria:
        lines.append("## Acceptance criteria")
        for c in criteria:
            lines.append(f"- [ ] {c}")
        lines.append("")

    if task.get("validation"):
        lines.append("## Validation")
        lines.append(f"```\n{task['validation']}\n```")

    return "\n".join(lines)


class GitHubOrchestrator:
    """Controls the full project lifecycle: planning → issue creation → PR review."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.gh = GitHubClient()
        self._trace_ctx: dict[str, Any] = {
            "project_id": project_id,
            "actor": "orchestrator",
            "stage": "orchestration",
        }
        self._log_seq = 0

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _project(self) -> dict[str, Any]:
        p = await get_project(self.project_id)
        if not p:
            raise RuntimeError(f"Project {self.project_id} not found")
        return p

    async def _llm_config(self) -> dict[str, Any]:
        p = await self._project()
        return normalize_llm_config(p.get("llm_config_json") or p.get("llm_config") or {})

    def _set_trace(self, stage: str, actor: str = "orchestrator") -> None:
        self._trace_ctx["stage"] = stage
        self._trace_ctx["actor"] = actor

    async def _log(self, task_text: str, output: str = "", status: str = "INFO") -> None:
        self._log_seq += 1
        await emit_builder_log(
            self.project_id,
            task_index=self._log_seq,
            task_text=task_text,
            output=output,
            status=status,
        )
        await save_activity_log(
            self.project_id,
            seq=self._log_seq,
            task_text=task_text,
            output=output,
            status=status,
        )

    # ── Stage 1: Analyze repo and produce plan ────────────────────────────────

    async def analyze_and_plan(self) -> None:
        """Read the repo, call architect LLM, stream the plan, wait for approval."""
        project = await self._project()

        # Parse owner/repo from stored repo_url or github_repo_owner/name
        owner = project.get("github_repo_owner")
        repo = project.get("github_repo_name")
        if not owner or not repo:
            repo_url = project.get("repo_url") or project.get("intake_text", "")
            parsed = _parse_repo_url(repo_url)
            if not parsed:
                raise RuntimeError("Cannot parse GitHub owner/repo from project")
            owner, repo = parsed
            # If no owner in URL (just a bare repo name), default to the configured org
            if owner == repo or not owner:
                org = settings.github_default_org or settings.github_owner
                owner = org
            await update_project(
                self.project_id,
                github_repo_owner=owner,
                github_repo_name=repo,
            )

        # Auto-register webhook so PR events are delivered without manual setup
        webhook_result = await self.gh.register_webhook(owner, repo)
        if webhook_result == "registered":
            await self._log("Webhook registered", f"PR events → {settings.automatron_public_url}/api/webhooks/github", "SUCCESS")
        elif webhook_result == "already_exists":
            await self._log("Webhook already registered", f"{owner}/{repo}", "INFO")
        elif webhook_result.startswith("error:"):
            await self._log("Webhook registration failed", webhook_result, "AMBIGUITY")
        # "skipped" means AUTOMATRON_PUBLIC_URL not set — silent, user hasn't configured it

        self._set_trace("planning", "architect")
        await trace_event(
            self.project_id, "orchestrator", "architect.run.started",
            {"owner": owner, "repo": repo},
        )

        await self._log("Reading repository files", f"{owner}/{repo}")

        # Read repo context
        readme = await self.gh.read_file(owner, repo, "README.md") or ""
        prd = await self.gh.read_file(owner, repo, "docs/PRD.md") or ""
        extra_context = f"\n\n---\n\n{prd}" if prd else ""

        context_parts = []
        if readme:
            context_parts.append("README.md")
        if prd:
            context_parts.append("docs/PRD.md")
        await self._log(
            "Repository context loaded",
            ", ".join(context_parts) if context_parts else "No README found",
            "SUCCESS" if context_parts else "AMBIGUITY",
        )

        # Fetch optional Figma design context (from URLs)
        figma_context = ""
        figma_urls = project.get("figma_urls") or []
        if figma_urls and settings.figma_access_token:
            await self._log("Fetching Figma design context", f"{len(figma_urls)} URL(s)", "RUNNING")
            figma_context = await _fetch_figma_context(figma_urls, settings.figma_access_token)
            await self._log("Figma context loaded", f"{len(figma_context)} chars", "INFO")
        elif figma_urls and not settings.figma_access_token:
            await self._log("Figma URLs present but FIGMA_ACCESS_TOKEN not set", "", "AMBIGUITY")

        # Append Figma file context if a .fig was uploaded
        figma_file_context = (project.get("figma_file_context") or "").strip()
        if figma_file_context:
            figma_context = (figma_context + "\n\n" + figma_file_context).strip()
            await self._log("Figma file context included", f"{len(figma_file_context)} chars", "INFO")

        system_prompt = _load_prompt("architect_github_v1.txt")
        user_msg = (
            f"Repository: {owner}/{repo}\n\n"
            f"## README\n\n{readme}{extra_context}\n\n"
        )
        if figma_context:
            user_msg += f"## Figma Design Context\n\n{figma_context}\n\n"
        user_msg += "Produce the architecture document, stories document, and issue plan now."

        llm_cfg = await self._llm_config()
        model = llm_cfg["architect"]["model"]

        await self._log("Architect LLM generating plan", f"Model: {model}", "RUNNING")

        # Stream tokens to UI
        full_response = ""
        async for chunk in call_llm_streaming(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_msg)],
            model=model,
            max_tokens=32768,
            trace_context={**self._trace_ctx, "actor": "architect", "prompt_name": "architect_github_v1"},
        ):
            full_response += chunk
            await emit_architect_chunk(self.project_id, chunk)

        # Parse blocks
        architecture_md = _parse_tagged_block(full_response, "markdown:architecture") or ""
        stories_md = _parse_tagged_block(full_response, "markdown:stories") or ""
        issue_plan_raw = _parse_tagged_block(full_response, "json:issue_plan") or "{}"

        try:
            issue_plan = json.loads(issue_plan_raw)
        except json.JSONDecodeError:
            logger.warning(
                "architect: json:issue_plan block not parseable — response length=%d, block_raw=%r",
                len(full_response), issue_plan_raw[:200],
            )
            issue_plan = {}

        # Build a human-readable plan_md from the issue plan
        plan_md = _build_plan_md(issue_plan, architecture_md, stories_md)

        epics = len(issue_plan.get("epics", []))
        tasks = _count_tasks(issue_plan)
        await self._log(
            "Plan generated — awaiting your approval",
            f"{epics} epics · {tasks} tasks",
            "SUCCESS",
        )

        await update_project(
            self.project_id,
            plan_md=plan_md,
            issue_plan_json=json.dumps(issue_plan),
            project_stage="awaiting_plan_approval",
            status="planning",
        )

        await emit_architect_message(self.project_id, plan_md, streaming=False)
        await emit_status_update(
            self.project_id,
            status="planning",
            stage="awaiting_plan_approval",
            progress={"completed": 0, "total": _count_tasks(issue_plan)},
        )
        await emit_human_required(
            self.project_id,
            "Review the plan and approve to create GitHub Issues.",
            stage="awaiting_plan_approval",
        )

        await trace_event(
            self.project_id, "architect", "architect.run.completed",
            {"epics": len(issue_plan.get("epics", []))},
        )

    # ── Stage 2: Push docs + create milestones + issues ───────────────────────

    async def apply_plan(self) -> None:
        """Push docs to repo, create GitHub Milestones and Issues."""
        project = await self._project()
        owner = project["github_repo_owner"]
        repo = project["github_repo_name"]
        issue_plan_raw = project.get("issue_plan_json") or project.get("execution_contract_json") or "{}"
        issue_plan = json.loads(issue_plan_raw) if isinstance(issue_plan_raw, str) else issue_plan_raw

        if not issue_plan.get("epics"):
            raise RuntimeError("No issue plan found — re-run planning first")

        self._set_trace("apply_plan", "orchestrator")
        await trace_event(self.project_id, "orchestrator", "apply_plan.started", {})

        await update_project(self.project_id, project_stage="building", status="building")
        await self._log("Plan approved — starting issue creation", f"{owner}/{repo}")

        # Push architecture + stories docs
        architecture_md = _parse_tagged_block(project.get("plan_md", ""), "markdown:architecture") or ""
        stories_md = _parse_tagged_block(project.get("plan_md", ""), "markdown:stories") or ""

        # Re-read from plan_md if parsing failed (plan_md is the rendered summary)
        # Fall back: read directly from DB columns if available
        if not architecture_md:
            architecture_md = project.get("architecture_md", "")
        if not stories_md:
            stories_md = project.get("stories_md", "")

        default_branch = project.get("default_branch") or "main"

        docs_pushed = []
        for path, content, label in [
            ("docs/ARCHITECTURE.md", architecture_md, "architecture"),
            ("docs/STORIES.md", stories_md, "stories"),
        ]:
            if content:
                try:
                    await self.gh.push_file(
                        owner, repo, path, content,
                        f"docs: add {label} by Automatron",
                        branch=default_branch,
                    )
                    docs_pushed.append(path)
                except Exception as exc:
                    logger.warning("Could not push %s: %s", path, exc)

        if docs_pushed:
            await self._log("Docs pushed to repository", ", ".join(docs_pushed), "SUCCESS")

        # Create milestones and issues
        total = _count_tasks(issue_plan)
        created = 0
        await self._log("Creating GitHub milestones and issues", f"{total} tasks planned")

        for epic in issue_plan.get("epics", []):
            epic_title = epic.get("title", "Untitled Epic")
            milestone_number: int | None = None
            try:
                milestone_number = await self.gh.create_milestone(
                    owner, repo, epic_title, epic.get("description", "")
                )
                await self._log(f"Milestone: {epic_title}", f"#{milestone_number}", "INFO")
            except Exception as exc:
                logger.warning("Could not create milestone '%s': %s", epic_title, exc)
                await self._log(f"Milestone skipped: {epic_title}", str(exc), "AMBIGUITY")

            # Ensure story label exists (GitHub caps label names at 50 chars)
            for story in epic.get("stories", []):
                story_title = story.get("title", "")
                if story_title:
                    label_name = story_title[:50]
                    try:
                        await self.gh.ensure_label(owner, repo, label_name, "bfd4f2")
                    except Exception as exc:
                        logger.warning("Could not ensure label '%s': %s", label_name, exc)

                for task in story.get("tasks", []):
                    task_title = task.get("title", "Untitled Task")
                    body = _render_issue_body(task, epic_title, story_title)
                    # GitHub label names are capped at 50 chars
                    label_name = story_title[:50] if story_title else ""
                    labels = [label_name] if label_name else []

                    try:
                        try:
                            gh_issue = await self.gh.create_issue(
                                owner, repo,
                                title=task_title,
                                body=body,
                                milestone_number=milestone_number,
                                labels=labels,
                                assignees=["copilot"],
                            )
                        except Exception:
                            try:
                                # Copilot agent not enabled on this repo — create without assignee
                                gh_issue = await self.gh.create_issue(
                                    owner, repo,
                                    title=task_title,
                                    body=body,
                                    milestone_number=milestone_number,
                                    labels=labels,
                                )
                            except Exception:
                                # Milestone or label invalid — bare create
                                gh_issue = await self.gh.create_issue(
                                    owner, repo,
                                    title=task_title,
                                    body=body,
                                )
                        issue_number = gh_issue["number"]
                        cw_url = gh_issue["html_url"]
                        await create_github_issue(
                            str(uuid.uuid4()),
                            self.project_id,
                            issue_number,
                            task_title,
                            epic=epic_title,
                            story=story_title or None,
                            copilot_workspace_url=cw_url,
                        )
                        created += 1
                        await self._log(
                            f"Issue #{issue_number}: {task_title}",
                            epic_title,
                            "SUCCESS",
                        )
                    except Exception as exc:
                        logger.error("Failed to create issue '%s': %s", task_title, exc)
                        await self._log(f"Failed: {task_title}", str(exc), "BLOCKER")

        await self._log(
            f"{created}/{total} issues created on GitHub",
            "Assign to Copilot to start building" if created else "Check errors above",
            "SUCCESS" if created == total else "AMBIGUITY",
        )

        issues = await list_github_issues(self.project_id)
        await emit_issues_updated(self.project_id, issues)
        await emit_status_update(
            self.project_id,
            status="building",
            stage="building",
            progress={"completed": 0, "total": total},
        )
        await trace_event(
            self.project_id, "orchestrator", "apply_plan.completed",
            {"issues_created": created, "total_tasks": total},
        )

    # ── Stage 3: Sync issue/PR status from GitHub ─────────────────────────────

    async def sync_issues(self) -> None:
        """Poll GitHub for issue/PR state and update the local DB."""
        project = await self._project()
        owner = project.get("github_repo_owner")
        repo = project.get("github_repo_name")
        if not owner or not repo:
            return

        local_issues = await list_github_issues(self.project_id)
        if not local_issues:
            return

        issue_map = {i["issue_number"]: i for i in local_issues}

        # Fetch current state of all tracked issues from GitHub
        gh_issues = await self.gh.list_issues(owner, repo, state="all")
        gh_map = {i["number"]: i for i in gh_issues}

        for number, local in issue_map.items():
            gh = gh_map.get(number)
            if not gh:
                continue

            new_status = local["status"]

            if gh["state"] == "closed":
                new_status = "merged" if local.get("pr_number") else "closed"
            elif local["status"] == "open":
                # Check for a linked PR if we don't already know about one
                if not local.get("pr_number"):
                    pr = await self.gh.find_pr_for_issue(owner, repo, number)
                    if pr:
                        await update_github_issue_pr(
                            self.project_id, number,
                            pr["number"], pr["html_url"],
                            status="pr_open",
                        )
                        new_status = "pr_open"
                        continue

            if new_status != local["status"]:
                await update_github_issue_status(self.project_id, number, new_status)

        issues = await list_github_issues(self.project_id)
        done = sum(1 for i in issues if i["status"] in ("merged", "closed"))
        await emit_issues_updated(self.project_id, issues)
        await emit_status_update(
            self.project_id,
            status="building",
            stage="building",
            progress={"completed": done, "total": len(issues)},
        )

        # If all issues are done and we don't have a preview URL yet, auto-detect from GitHub Deployments
        project = await self._project()
        if done == len(issues) and done > 0 and not project.get("preview_url"):
            await self._detect_and_set_preview_url(owner, repo)

    async def _detect_and_set_preview_url(self, owner: str, repo: str) -> None:
        """Clone the repo locally and run it in Docker for a live preview."""
        from orchestrator.preview import run_preview_locally
        await self._log("Starting local preview", f"Cloning and building {owner}/{repo}", "RUNNING")
        preview_url = await run_preview_locally(str(self.project_id), owner, repo)
        if preview_url:
            await update_project_preview(self.project_id, preview_url, "ready")
            await self._log("Preview ready", preview_url, "SUCCESS")
            await emit_status_update(
                self.project_id,
                status="building",
                stage="building",
                preview_url=preview_url,
            )
        else:
            await self._log("Preview failed", "Could not build or run the project locally", "ERROR")

    # ── Stage 4: AI review a PR ───────────────────────────────────────────────

    async def review_pr(self, issue_number: int, pr_number: int) -> None:
        """Fetch the PR diff, call the reviewer LLM, post a comment, store the result."""
        project = await self._project()
        owner = project["github_repo_owner"]
        repo = project["github_repo_name"]

        self._set_trace("pr_review", "reviewer")
        await self._log(f"Reviewing PR #{pr_number}", f"Issue #{issue_number} · {owner}/{repo}")

        # Get diff and task spec (issue body)
        diff = await self.gh.get_pr_diff(owner, repo, pr_number)
        issue = await self.gh.get_issue(owner, repo, issue_number)
        issue_body = issue.get("body", "")

        llm_cfg = await self._llm_config()
        model = llm_cfg["reviewer"]["model"]
        await self._log(
            f"Running reviewer LLM on PR #{pr_number}",
            f"Model: {model} · diff {len(diff)} chars",
            "RUNNING",
        )

        system_prompt = (
            "You are a code reviewer. You will receive a GitHub issue (the task specification) "
            "and the PR diff that implements it.\n\n"
            "Review the diff against the acceptance criteria and implementation notes.\n"
            "Respond with:\n"
            "1. **PASSED** or **ISSUES FOUND** on the first line\n"
            "2. A brief summary (2-3 sentences)\n"
            "3. Bullet list of specific issues (if any), referencing file paths and line numbers\n"
            "4. Bullet list of what was done well\n\n"
            "Be concise. Focus on correctness and completeness vs the spec, not style."
        )
        user_msg = (
            f"## Issue #{issue_number}: {issue.get('title', '')}\n\n"
            f"{issue_body}\n\n"
            f"---\n\n## PR Diff\n\n```diff\n{diff[:12000]}\n```"
        )

        try:
            review_text = await call_llm(
                [SystemMessage(content=system_prompt), HumanMessage(content=user_msg)],
                model=model,
                max_tokens=2048,
                trace_context={**self._trace_ctx, "actor": "reviewer"},
            )
        except Exception as exc:
            await emit_error(self.project_id, f"PR review failed: {exc}")
            return

        passed = review_text.strip().upper().startswith("PASSED")
        await self._log(
            f"PR #{pr_number} review: {'PASSED' if passed else 'ISSUES FOUND'}",
            review_text[:200].replace("\n", " "),
            "SUCCESS" if passed else "BLOCKER",
        )
        review_data = {
            "passed": passed,
            "summary": review_text,
            "pr_number": pr_number,
            "issue_number": issue_number,
        }

        await update_github_issue_pr(
            self.project_id, issue_number, pr_number,
            issue.get("html_url", ""),
            status="pr_reviewed",
            pr_review=review_data,
        )

        # Post review as a comment on the PR
        review_comment = (
            f"## Automatron AI Review\n\n{review_text}\n\n"
            f"*Reviewed by Automatron (model: {model})*"
        )
        try:
            await self.gh.post_pr_comment(owner, repo, pr_number, review_comment)
        except Exception as exc:
            logger.warning("Could not post PR comment: %s", exc)

        await emit_pr_review_ready(
            self.project_id, issue_number, pr_number, passed, review_text
        )
        await trace_event(
            self.project_id, "reviewer", "pr.review.completed",
            {"issue_number": issue_number, "pr_number": pr_number, "passed": passed},
        )

    # ── Stage 5: Audit codebase and create fix issues ────────────────────────

    async def audit_codebase(self) -> None:
        """Reviewer scans the current code, architect generates new fix issues."""
        import os

        project = await self._project()
        owner = project["github_repo_owner"]
        repo = project["github_repo_name"]

        self._set_trace("audit", "reviewer")
        await self._log("Starting code audit", f"{owner}/{repo}", "RUNNING")

        # Read source files from local workspace clone if available
        workspace_dir = settings.workspace_base_dir / str(self.project_id) / "repo"
        if workspace_dir.exists():
            code_context = _read_source_files(workspace_dir)
            await self._log("Source files loaded", f"{len(code_context)} chars from workspace", "INFO")
        else:
            readme = await self.gh.read_file(owner, repo, "README.md") or ""
            code_context = f"### README.md\n{readme}"
            await self._log("Source files loaded", "from GitHub README (no local clone)", "INFO")

        # Build context from existing issues and reviews
        existing_issues = await list_github_issues(self.project_id)
        plan_summary = "\n".join(
            f"- #{i['issue_number']}: {i['title']} [{i['status']}]"
            for i in existing_issues
        )
        review_notes = []
        for i in existing_issues:
            review = i.get("pr_review_json") or {}
            if isinstance(review, dict) and not review.get("passed") and review.get("summary"):
                review_notes.append(
                    f"Issue #{i['issue_number']} ({i['title']}): {review['summary'][:400]}"
                )

        llm_cfg = await self._llm_config()
        reviewer_model = llm_cfg["reviewer"]["model"]
        architect_model = llm_cfg["architect"]["model"]

        # ── Reviewer: identify what's missing / broken ───────────────────────
        await self._log("Reviewer scanning codebase", f"Model: {reviewer_model}", "RUNNING")

        reviewer_system = (
            "You are an expert code reviewer. You will receive the original task plan and "
            "the current state of the codebase.\n\n"
            "Identify:\n"
            "1. Features from the plan that were not implemented\n"
            "2. Code that is incomplete, broken, or is just the default scaffold\n"
            "3. Critical bugs or architectural problems\n\n"
            "Be specific: reference file paths and exactly what is missing or wrong.\n"
            "Output a numbered list of findings. Be concise.\n\n"
            "IMPORTANT — Fix strategy:\n"
            "If the project already has substantial working code, prioritise fixes that require "
            "the MINIMUM change to the existing codebase. Do not propose rewriting or replacing "
            "code that already works. Prefer patching, adding missing pieces, or swapping a single "
            "dependency over full rewrites. Only recommend a rewrite if the existing code is "
            "fundamentally incompatible and cannot be patched."
        )
        reviewer_user = (
            f"## Original Task Plan\n\n{plan_summary}\n\n"
            + (f"## Failed PR Reviews\n\n" + "\n\n".join(review_notes) + "\n\n" if review_notes else "")
            + f"## Current Codebase\n\n{code_context[:30000]}"
        )

        try:
            findings = await call_llm(
                [SystemMessage(content=reviewer_system), HumanMessage(content=reviewer_user)],
                model=reviewer_model,
                max_tokens=4096,
                trace_context={**self._trace_ctx, "actor": "reviewer"},
            )
        except Exception as exc:
            await emit_error(self.project_id, f"Audit review failed: {exc}")
            return

        await self._log("Reviewer findings", findings[:300].replace("\n", " "), "BLOCKER")

        # ── Architect: generate fix issues ────────────────────────────────────
        self._set_trace("audit", "architect")
        await self._log("Architect generating fix issues", f"Model: {architect_model}", "RUNNING")

        arch_system = _load_prompt("architect_github_v1.txt")
        arch_user = (
            f"## Project: {project.get('name', repo)}\n\n"
            f"The codebase has been reviewed. The following problems were found:\n\n"
            f"{findings}\n\n"
            "Generate a json:issue_plan with specific tasks to fix each problem. "
            "Focus only on the issues found above. Each task must fix one specific problem.\n\n"
            "IMPORTANT — Minimum-change principle: if the project already has substantial working "
            "code, each task must patch or extend the existing code rather than replace it. "
            "Do not generate tasks that rewrite working features from scratch. "
            "A task should change only what is necessary to fix the reported problem."
        )

        full_response = ""
        async for chunk in call_llm_streaming(
            [SystemMessage(content=arch_system), HumanMessage(content=arch_user)],
            model=architect_model,
            max_tokens=16384,
            trace_context={**self._trace_ctx, "actor": "architect"},
        ):
            full_response += chunk
            await emit_architect_chunk(self.project_id, chunk)

        await emit_architect_message(self.project_id, full_response)

        issue_plan_raw = _parse_tagged_block(full_response, "json:issue_plan")
        if not issue_plan_raw:
            await emit_error(self.project_id, "Architect did not return a valid issue plan")
            return

        try:
            issue_plan = json.loads(issue_plan_raw)
        except json.JSONDecodeError as exc:
            await emit_error(self.project_id, f"Issue plan JSON parse error: {exc}")
            return

        # ── Create issues on GitHub ───────────────────────────────────────────
        total = _count_tasks(issue_plan)
        await self._log("Creating fix issues on GitHub", f"{total} tasks planned", "RUNNING")

        created = 0
        for epic in issue_plan.get("epics", []):
            epic_title = epic.get("title", "Untitled Epic")
            milestone_number: int | None = None
            try:
                milestone_number = await self.gh.create_milestone(owner, repo, epic_title)
            except Exception:
                pass

            for story in epic.get("stories", []):
                story_title = story.get("title", "")
                for task in story.get("tasks", []):
                    task_title = task.get("title", "Untitled Task")
                    body = _render_issue_body(task, epic_title, story_title)
                    try:
                        gh_issue = await self.gh.create_issue(
                            owner, repo,
                            title=task_title,
                            body=body,
                            milestone_number=milestone_number,
                        )
                        issue_num = gh_issue["number"]
                        await create_github_issue(
                            str(uuid.uuid4()),
                            str(self.project_id),
                            issue_num,
                            task_title,
                            epic=epic_title,
                            story=story_title or None,
                            copilot_workspace_url=gh_issue["html_url"],
                        )
                        created += 1
                        await self._log(f"Issue #{issue_num}: {task_title}", "", "SUCCESS")
                    except Exception as exc:
                        logger.warning("Failed to create issue '%s': %s", task_title, exc)

        issues = await list_github_issues(self.project_id)
        await emit_issues_updated(self.project_id, issues)
        await self._log(
            f"Audit complete — {created}/{total} fix issues created",
            "Use 'Assign Copilot' to start fixing",
            "SUCCESS",
        )


# ── Module-level runner functions (called by API routes) ──────────────────────

async def start_project(project_id: str) -> None:
    """Entry point: read repo and generate the plan."""
    try:
        orch = GitHubOrchestrator(project_id)
        await orch.analyze_and_plan()
    except Exception as exc:
        logger.error("start_project failed for %s: %s", project_id, exc)
        await update_project(project_id, status="error", project_stage="error")
        await emit_error(project_id, f"{type(exc).__name__}: {exc}")
        raise


async def resume_project(project_id: str, approval_type: str, approved: bool = True) -> None:
    """Resume after a human approval gate."""
    await record_approval(project_id, approval_type, approved)
    if approval_type == "plan" and approved:
        try:
            orch = GitHubOrchestrator(project_id)
            await orch.apply_plan()
        except Exception as exc:
            logger.error("resume_project (plan) failed for %s: %s", project_id, exc)
            await update_project(project_id, status="error", project_stage="error")
            await emit_error(project_id, f"{type(exc).__name__}: {exc}")
            raise
    elif approval_type == "preview" and approved:
        try:
            await orch_plan_deployment(project_id)
        except Exception as exc:
            logger.error("resume_project (preview) failed for %s: %s", project_id, exc)
            await update_project(project_id, project_stage="error")
            await emit_error(project_id, f"{type(exc).__name__}: {exc}")
            raise


async def orch_plan_deployment(project_id: str) -> None:
    """Run stack detection and store an initial DeploymentProfile.

    Called when the user approves the preview. Sets the project into
    `deployment_planning` so the UI can collect the deploy target config.
    """
    from orchestrator.deployment_v2 import get_strategy
    from orchestrator.models.project import update_project_deployment

    project = await get_project(project_id)
    if not project:
        raise RuntimeError(f"Project not found: {project_id}")

    strategy = get_strategy("kamal")
    repo_files = await _fetch_detector_files(project, strategy)
    profile = strategy.detect_requirements(project, repo_files=repo_files)

    await update_project_deployment(
        project_id,
        deployment_strategy="kamal",
        deployment_profile=profile.to_dict(),
    )
    await update_project(project_id, project_stage="deployment_planning")
    await save_activity_log(
        project_id,
        seq=int(__import__("time").time()),
        task_text=f"Detected stack: framework={profile.framework}, "
        f"package_manager={profile.package_manager}, next_output={profile.next_output}",
        status="INFO",
    )


async def _fetch_detector_files(project: dict[str, Any], strategy: Any) -> dict[str, str]:
    owner = project.get("github_repo_owner") or ""
    repo = project.get("github_repo_name") or ""
    if not owner or not repo:
        return {}
    client = GitHubClient()
    files: dict[str, str] = {}
    candidates = list(strategy.template_probe_files()) if hasattr(strategy, "template_probe_files") else []
    for path in candidates:
        try:
            content = await client.read_file(owner, repo, path)
        except Exception as exc:  # pragma: no cover — network errors
            logger.debug("read_file %s/%s/%s failed: %s", owner, repo, path, exc)
            continue
        if content is not None:
            files[path] = content
    return files


def _build_deploy_audit_issue_body(
    project: dict[str, Any],
    profile: Any,
    fingerprint: dict[str, Any],
) -> str:
    secret_names = list(project.get("deployment_secret_names") or [])
    stack = {
        "framework": getattr(profile, "framework", "unknown"),
        "package_manager": getattr(profile, "package_manager", "unknown"),
        "router_style": getattr(profile, "router_style", "unknown"),
        "next_output": getattr(profile, "next_output", "unknown"),
    }
    profile_summary = {
        "strategy": getattr(profile, "strategy", "kamal"),
        "host": getattr(profile, "host", ""),
        "domain": getattr(profile, "domain", ""),
        "ssh_user": getattr(profile, "ssh_user", ""),
        "ssh_port": getattr(profile, "ssh_port", 22),
        "container_port": getattr(profile, "container_port", 3000),
        "health_path": getattr(profile, "health_path", "/api/health"),
        "registry": getattr(profile, "registry", "ghcr.io"),
        "registry_username": getattr(profile, "registry_username", ""),
        "image": getattr(profile, "image", ""),
        "artifacts_push_mode": getattr(profile, "artifacts_push_mode", "pr"),
        "auto_deploy_on_main": bool(getattr(profile, "auto_deploy_on_main", False)),
    }

    return (
        "## Deploy Readiness Audit\n\n"
        "Automatron generated deterministic deployment artifacts. "
        "Validate readiness for production deploy and adapt to repository specifics.\n\n"
        "### Context\n"
        f"- Project: `{project.get('name')}`\n"
        f"- Repository: `{project.get('github_repo_owner')}/{project.get('github_repo_name')}`\n\n"
        "### Stack detection summary\n"
        f"```json\n{json.dumps(stack, indent=2)}\n```\n\n"
        "### Deployment profile (safe fields)\n"
        f"```json\n{json.dumps(profile_summary, indent=2)}\n```\n\n"
        "### Deployment secrets (names only)\n"
        f"```json\n{json.dumps(secret_names, indent=2)}\n```\n\n"
        "### Artifact fingerprint\n"
        f"```json\n{json.dumps(fingerprint, indent=2)}\n```\n\n"
        "### Strict checklist\n"
        "- [ ] Health endpoint exists and matches configured `health_path`\n"
        "- [ ] Startup command/CMD is correct for production runtime\n"
        "- [ ] Required env vars are documented; secret names are complete\n"
        "- [ ] Domain/host/port config is internally consistent\n"
        "- [ ] Dockerfile and Kamal config align with stack specifics\n"
        "- [ ] Deploy workflow inputs and action paths are valid\n"
        "- [ ] Rollback preconditions and image tag strategy are valid\n\n"
        "### Agent instructions\n"
        "1. Identify all readiness gaps blocking high-quality deploy.\n"
        "2. If fixes are needed, open PR(s) with concrete changes.\n"
        "3. Keep this issue open until readiness is confirmed.\n"
        "4. Close this issue only when deploy readiness is fully confirmed.\n"
    )


async def _read_deploy_audit_issue_state(project: dict[str, Any]) -> dict[str, Any]:
    owner = (project.get("github_repo_owner") or "").strip()
    repo = (project.get("github_repo_name") or "").strip()
    issue_number = project.get("deploy_audit_issue_number")
    issue_url = project.get("deploy_audit_issue_url")

    if not owner or not repo:
        return {
            "ok": False,
            "code": "deploy_audit_issue_missing",
            "gate_status": "missing",
            "state": "missing",
            "issue_number": issue_number,
            "issue_url": issue_url,
            "message": "Project has no GitHub repo configured",
        }
    if not issue_number:
        return {
            "ok": False,
            "code": "deploy_audit_issue_missing",
            "gate_status": "missing",
            "state": "missing",
            "issue_number": None,
            "issue_url": None,
            "message": "Deploy audit issue is not created yet",
        }

    gh = GitHubClient()
    try:
        issue = await gh.get_issue(owner, repo, int(issue_number))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return {
                "ok": False,
                "code": "deploy_audit_issue_missing",
                "gate_status": "missing",
                "state": "missing",
                "issue_number": int(issue_number),
                "issue_url": issue_url,
                "message": "Deploy audit issue no longer exists on GitHub",
            }
        raise

    state = (issue.get("state") or "").lower()
    gate_status = "ready" if state == "closed" else "pending"
    return {
        "ok": state == "closed",
        "code": None if state == "closed" else "deploy_audit_issue_open",
        "gate_status": gate_status,
        "state": state or "unknown",
        "issue_number": int(issue.get("number") or issue_number),
        "issue_url": issue.get("html_url") or issue_url,
        "message": (
            "Deploy audit issue is closed and gate is satisfied"
            if state == "closed"
            else "Deploy audit issue is still open"
        ),
    }


async def _upsert_deploy_audit_issue(
    *,
    project_id: str,
    project: dict[str, Any],
    profile: Any,
    fingerprint: dict[str, Any],
) -> dict[str, Any]:
    from orchestrator.models.project import update_project_deployment

    owner = (project.get("github_repo_owner") or "").strip()
    repo = (project.get("github_repo_name") or "").strip()
    if not owner or not repo:
        raise RuntimeError("Project has no GitHub repo configured")

    gh = GitHubClient()
    await gh.ensure_label(owner, repo, _DEPLOY_AUDIT_LABEL, color="1d76db")
    body = _build_deploy_audit_issue_body(project, profile, fingerprint)
    issue_number = project.get("deploy_audit_issue_number")
    issue: dict[str, Any] | None = None

    if issue_number:
        try:
            candidate = await gh.get_issue(owner, repo, int(issue_number))
            if (candidate.get("state") or "").lower() == "open":
                issue = await gh.update_issue(
                    owner,
                    repo,
                    int(issue_number),
                    title=_DEPLOY_AUDIT_ISSUE_TITLE,
                    body=body,
                    labels=[_DEPLOY_AUDIT_LABEL],
                )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

    if issue is None:
        issue = await gh.create_issue(
            owner,
            repo,
            title=_DEPLOY_AUDIT_ISSUE_TITLE,
            body=body,
            labels=[_DEPLOY_AUDIT_LABEL],
        )

    state = (issue.get("state") or "open").lower()
    gate_status = "ready" if state == "closed" else "pending"
    issue_number = int(issue.get("number"))
    issue_url = issue.get("html_url")

    await update_project_deployment(
        project_id,
        deploy_audit_issue_number=issue_number,
        deploy_audit_issue_url=issue_url,
        deploy_audit_gate_status=gate_status,
    )

    try:
        await gh.trigger_copilot_agent(owner, repo, issue_number)
    except Exception as exc:  # pragma: no cover - network/provider variance
        logger.warning(
            "Failed to auto-assign Copilot for deploy audit issue #%s in %s/%s: %s",
            issue_number,
            owner,
            repo,
            exc,
        )

    return {
        "number": issue_number,
        "url": issue_url,
        "state": state,
        "gate_status": gate_status,
    }


async def orch_generate_deploy_artifacts(project_id: str) -> dict[str, Any]:
    """Render and push deployment artifacts to the child repo."""
    from orchestrator.deployment_v2 import get_strategy
    from orchestrator.deployment_v2.artifacts import ArtifactPusher
    from orchestrator.deployment_v2.profile import DeploymentProfile
    from orchestrator.models.project import update_project_deployment

    project = await get_project(project_id)
    if not project:
        raise RuntimeError(f"Project not found: {project_id}")

    strategy_name = (project.get("deployment_strategy") or "").strip()
    if strategy_name != "kamal":
        raise RuntimeError("Project is not configured for Kamal deployment")

    strategy = get_strategy(strategy_name)
    profile_data = project.get("deployment_profile") or {}
    if not profile_data:
        raise RuntimeError("Deployment profile is not set; run plan-deployment first")
    profile = DeploymentProfile.from_dict(profile_data)

    artifacts = strategy.render_artifacts(profile)
    workflows = strategy.workflow_files(profile)
    files = {**artifacts, **workflows}

    owner = project.get("github_repo_owner") or ""
    repo = project.get("github_repo_name") or ""
    if not owner or not repo:
        raise RuntimeError("Project has no GitHub repo configured")

    pusher = ArtifactPusher(strategy_version=strategy.version)
    fingerprint = await pusher.push(
        owner=owner,
        repo=repo,
        profile=profile,
        files=files,
        mode=profile.artifacts_push_mode,
    )

    fingerprint_dict = fingerprint.to_dict()
    await update_project_deployment(
        project_id,
        deploy_artifacts_fingerprint=fingerprint_dict,
    )
    deploy_audit_issue = await _upsert_deploy_audit_issue(
        project_id=project_id,
        project=project,
        profile=profile,
        fingerprint=fingerprint_dict,
    )
    await update_project(project_id, project_stage="deployment_artifacts_generated")
    return {
        "fingerprint": fingerprint_dict,
        "deploy_audit_issue": deploy_audit_issue,
    }


async def orch_deploy(
    project_id: str,
    *,
    action: str = "deploy",
    rollback_to: str | None = None,
) -> dict[str, Any]:
    """Trigger a Kamal deploy/setup/rollback via workflow_dispatch."""
    from orchestrator.deployment_v2 import get_strategy
    from orchestrator.deployment_v2.profile import DeploymentProfile
    from orchestrator.github_actions.manager import GitHubActionsManager
    from orchestrator.models.project import (
        save_deploy_run,
        update_project_deployment,
        update_project_deploy_status,
    )

    if action not in {"setup", "deploy", "rollback"}:
        raise ValueError(f"Unknown deploy action: {action!r}")

    project = await get_project(project_id)
    if not project:
        raise RuntimeError(f"Project not found: {project_id}")

    if action == "rollback" and not _has_previous_successful_deploy(project):
        raise RuntimeError("rollback_no_previous_deploy")

    if action in {"setup", "deploy"}:
        gate = await _read_deploy_audit_issue_state(project)
        await update_project_deployment(
            project_id,
            deploy_audit_issue_number=gate.get("issue_number"),
            deploy_audit_issue_url=gate.get("issue_url"),
            deploy_audit_gate_status=gate.get("gate_status") or "missing",
        )
        if not gate.get("ok"):
            raise RuntimeError(str(gate.get("code") or "deploy_audit_issue_open"))

    strategy_name = (project.get("deployment_strategy") or "").strip()
    if strategy_name != "kamal":
        raise RuntimeError("Project is not configured for Kamal deployment")
    strategy = get_strategy(strategy_name)
    profile = DeploymentProfile.from_dict(project.get("deployment_profile") or {})

    automatron_run_id = uuid.uuid4().hex
    if action == "rollback" and not rollback_to:
        rollback_to = _last_successful_image_tag(project)

    inputs = strategy.dispatch_inputs(
        profile,
        action=action,  # type: ignore[arg-type]
        automatron_run_id=automatron_run_id,
        rollback_to=rollback_to,
    )

    repo_owner = (project.get("github_repo_owner") or "").strip()
    repo_short = (project.get("repo_name") or project.get("github_repo_name") or "").strip()
    repo_name = f"{repo_owner}/{repo_short}" if repo_owner and repo_short else repo_short
    if not repo_name:
        raise RuntimeError("Project has no repo_name")

    actions = GitHubActionsManager()
    await actions.dispatch_workflow(
        repo_name,
        ".github/workflows/deploy.yml",
        ref=project.get("default_branch") or "main",
        inputs=inputs,
    )

    await save_deploy_run(
        run_id=automatron_run_id,
        project_id=project_id,
        status="pending",
        branch=project.get("default_branch") or "main",
        output="",
        summary={"automatron_run_id": automatron_run_id, "action": action},
    )

    summary = await actions.match_run_by_correlation(
        repo_name,
        ".github/workflows/deploy.yml",
        automatron_run_id,
    )
    next_stage = "rolling_back" if action == "rollback" else "deploying"
    await update_project(project_id, project_stage=next_stage, status="deploying")
    await update_project_deploy_status(
        project_id,
        deploy_status=summary.status or "queued",
        last_deploy_run_id=summary.run_id,
        deploy_run_url=summary.run_url,
        deploy_commit_sha=summary.head_sha,
    )
    await update_project_deployment(
        project_id,
        automatron_deploy_run_id=automatron_run_id,
    )
    return {
        "automatron_run_id": automatron_run_id,
        "github_run_id": summary.run_id,
        "status": summary.status,
        "url": summary.run_url,
        "action": action,
    }


async def orch_sync_deploy(project_id: str) -> dict[str, Any]:
    """Refresh the latest workflow run status and update project stage."""
    from orchestrator.github_actions.manager import GitHubActionsManager
    from orchestrator.models.project import (
        update_project_deploy_status,
        upsert_deploy_run,
    )

    project = await get_project(project_id)
    if not project:
        raise RuntimeError(f"Project not found: {project_id}")

    repo_owner = (project.get("github_repo_owner") or "").strip()
    repo_short = (project.get("repo_name") or project.get("github_repo_name") or "").strip()
    repo_name = f"{repo_owner}/{repo_short}" if repo_owner and repo_short else repo_short
    run_id = project.get("last_deploy_run_id")
    if not repo_name or not run_id:
        return {"status": "not_configured"}

    actions = GitHubActionsManager()
    summary = await actions.get_workflow_run(repo_name, run_id)
    stage_map = {
        "deployed": "deployed",
        "failed": "deploy_failed",
        "running": "deploying",
        "queued": "deploying",
    }
    stage = stage_map.get(summary.status, project.get("project_stage") or "deploying")
    if project.get("project_stage") == "rolling_back":
        if summary.status == "deployed":
            stage = "rolled_back"
        elif summary.status == "failed":
            stage = "deploy_failed"
        else:
            stage = "rolling_back"

    automatron_run_id = project.get("automatron_deploy_run_id") or run_id
    await upsert_deploy_run(
        run_id=automatron_run_id,
        project_id=project_id,
        status=summary.status,
        branch=project.get("default_branch") or "main",
        output="",
        summary={
            "automatron_run_id": automatron_run_id,
            "github_run_id": summary.run_id,
            "url": summary.run_url,
            "head_sha": summary.head_sha,
        },
        deployed_at=summary.updated_at if summary.status == "deployed" else None,
    )
    await update_project_deploy_status(
        project_id,
        deploy_status=summary.status,
        last_deploy_run_id=summary.run_id,
        deploy_run_url=summary.run_url,
        deploy_commit_sha=summary.head_sha,
    )
    await update_project(project_id, project_stage=stage)
    return {
        "stage": stage,
        "status": summary.status,
        "url": summary.run_url,
        "github_run_id": summary.run_id,
    }


def _has_previous_successful_deploy(project: dict[str, Any]) -> bool:
    last_status = (project.get("deploy_status") or "").lower()
    return last_status == "deployed" and bool(project.get("deploy_commit_sha"))


def _last_successful_image_tag(project: dict[str, Any]) -> str:
    sha = project.get("deploy_commit_sha") or ""
    return str(sha)[:12] if sha else ""


async def sync_issues(project_id: str) -> None:
    orch = GitHubOrchestrator(project_id)
    await orch.sync_issues()


async def audit_project(project_id: str) -> None:
    orch = GitHubOrchestrator(project_id)
    await orch.audit_codebase()


async def assign_to_copilot(project_id: str) -> dict:
    """Assign all open issues in the project to the copilot agent."""
    orch = GitHubOrchestrator(project_id)
    project = await orch._project()
    owner = project.get("github_repo_owner")
    repo = project.get("github_repo_name")
    if not owner or not repo:
        raise RuntimeError("Project has no GitHub repo configured")

    issues = await list_github_issues(project_id)
    open_issues = [i for i in issues if i["status"] == "open"]

    await orch._log(
        f"Triggering Copilot agent on {len(open_issues)} open issues",
        f"{owner}/{repo}",
    )

    assigned, failed = 0, 0
    for issue in open_issues:
        try:
            await orch.gh.trigger_copilot_agent(owner, repo, issue["issue_number"])
            assigned += 1
            await orch._log(
                f"Copilot triggered on #{issue['issue_number']}",
                issue["title"],
                "SUCCESS",
            )
        except Exception as exc:
            logger.warning("Could not trigger copilot on issue #%s: %s", issue["issue_number"], exc)
            failed += 1
            await orch._log(
                f"Failed to trigger #{issue['issue_number']}",
                str(exc),
                "BLOCKER",
            )

    await orch._log(
        f"Copilot assignment complete: {assigned} triggered, {failed} failed",
        "",
        "SUCCESS" if failed == 0 else "AMBIGUITY",
    )
    logger.info("assign_to_copilot: %d assigned, %d failed (project=%s)", assigned, failed, project_id)
    return {"assigned": assigned, "failed": failed}


async def assign_to_copilot_issue(project_id: str, issue_number: int) -> dict:
    """Assign a single issue to the Copilot coding agent."""
    orch = GitHubOrchestrator(project_id)
    project = await orch._project()
    owner = project.get("github_repo_owner")
    repo = project.get("github_repo_name")
    if not owner or not repo:
        raise RuntimeError("Project has no GitHub repo configured")

    await orch.gh.trigger_copilot_agent(owner, repo, issue_number)
    await orch._log(f"Copilot assigned to #{issue_number}", f"{owner}/{repo}", "SUCCESS")
    return {"assigned": 1, "issue_number": issue_number}


async def review_pr(project_id: str, issue_number: int, pr_number: int) -> None:
    orch = GitHubOrchestrator(project_id)
    await orch.review_pr(issue_number, pr_number)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _count_tasks(issue_plan: dict) -> int:
    total = 0
    for epic in issue_plan.get("epics", []):
        for story in epic.get("stories", []):
            total += len(story.get("tasks", []))
    return total


def _build_plan_md(issue_plan: dict, architecture_md: str, stories_md: str) -> str:
    lines = ["# Project Plan\n"]

    if architecture_md:
        lines.append("## Architecture\n")
        lines.append(architecture_md)
        lines.append("")

    lines.append("## Tasks\n")
    for epic in issue_plan.get("epics", []):
        lines.append(f"### {epic.get('title', 'Epic')}\n")
        for story in epic.get("stories", []):
            lines.append(f"**{story.get('title', 'Story')}**\n")
            for task in story.get("tasks", []):
                lines.append(f"- [ ] {task.get('title', 'Task')}")
        lines.append("")

    return "\n".join(lines)
