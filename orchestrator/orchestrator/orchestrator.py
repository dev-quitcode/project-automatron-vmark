"""GitHub-native orchestrator — replaces LangGraph-based graph execution."""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from orchestrator.api.websocket import (
    emit_architect_chunk,
    emit_architect_message,
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
    update_github_issue_pr,
    update_github_issue_status,
    update_project,
)
from orchestrator.observability import trace_event

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent.parent / "prompts"


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
            await update_project(
                self.project_id,
                github_repo_owner=owner,
                github_repo_name=repo,
            )

        self._set_trace("planning", "architect")
        await trace_event(
            self.project_id, "orchestrator", "architect.run.started",
            {"owner": owner, "repo": repo},
        )

        # Read repo context
        readme = await self.gh.read_file(owner, repo, "README.md") or ""
        prd = await self.gh.read_file(owner, repo, "docs/PRD.md") or ""
        extra_context = f"\n\n---\n\n{prd}" if prd else ""

        system_prompt = _load_prompt("architect_github_v1.txt")
        user_msg = (
            f"Repository: {owner}/{repo}\n\n"
            f"## README\n\n{readme}{extra_context}\n\n"
            "Produce the architecture document, stories document, and issue plan now."
        )

        llm_cfg = await self._llm_config()
        model = llm_cfg["architect"]["model"]

        # Stream tokens to UI
        full_response = ""
        stream = await call_llm_streaming(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_msg)],
            model=model,
            max_tokens=16384,
            trace_context={**self._trace_ctx, "actor": "architect", "prompt_name": "architect_github_v1"},
        )
        async for chunk in stream:
            full_response += chunk
            await emit_architect_chunk(self.project_id, chunk)

        # Parse blocks
        architecture_md = _parse_tagged_block(full_response, "markdown:architecture") or ""
        stories_md = _parse_tagged_block(full_response, "markdown:stories") or ""
        issue_plan_raw = _parse_tagged_block(full_response, "json:issue_plan") or "{}"

        try:
            issue_plan = json.loads(issue_plan_raw)
        except json.JSONDecodeError:
            issue_plan = {}

        # Build a human-readable plan_md from the issue plan
        plan_md = _build_plan_md(issue_plan, architecture_md, stories_md)

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
                except Exception as exc:
                    logger.warning("Could not push %s: %s", path, exc)

        # Create milestones and issues
        total = _count_tasks(issue_plan)
        created = 0

        for epic in issue_plan.get("epics", []):
            epic_title = epic.get("title", "Untitled Epic")
            milestone_number: int | None = None
            try:
                milestone_number = await self.gh.create_milestone(
                    owner, repo, epic_title, epic.get("description", "")
                )
            except Exception as exc:
                logger.warning("Could not create milestone '%s': %s", epic_title, exc)

            # Ensure story label exists
            for story in epic.get("stories", []):
                story_title = story.get("title", "")
                if story_title:
                    try:
                        await self.gh.ensure_label(owner, repo, story_title, "bfd4f2")
                    except Exception:
                        pass

                for task in story.get("tasks", []):
                    task_title = task.get("title", "Untitled Task")
                    body = _render_issue_body(task, epic_title, story_title)
                    labels = [story_title] if story_title else []

                    try:
                        gh_issue = await self.gh.create_issue(
                            owner, repo,
                            title=task_title,
                            body=body,
                            milestone_number=milestone_number,
                            labels=labels,
                            assignees=["copilot"],
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
                    except Exception as exc:
                        logger.error("Failed to create issue '%s': %s", task_title, exc)

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

    # ── Stage 4: AI review a PR ───────────────────────────────────────────────

    async def review_pr(self, issue_number: int, pr_number: int) -> None:
        """Fetch the PR diff, call the reviewer LLM, post a comment, store the result."""
        project = await self._project()
        owner = project["github_repo_owner"]
        repo = project["github_repo_name"]

        self._set_trace("pr_review", "reviewer")

        # Get diff and task spec (issue body)
        diff = await self.gh.get_pr_diff(owner, repo, pr_number)
        issue = await self.gh.get_issue(owner, repo, issue_number)
        issue_body = issue.get("body", "")

        llm_cfg = await self._llm_config()
        model = llm_cfg["reviewer"]["model"]

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


async def sync_issues(project_id: str) -> None:
    orch = GitHubOrchestrator(project_id)
    await orch.sync_issues()


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
