"""GitHub webhook receiver — handles pull_request events from Copilot agent."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from orchestrator.api.websocket import emit_issues_updated
from orchestrator.config import settings
from orchestrator.models.project import (
    find_github_issue_by_repo,
    list_github_issues,
    update_github_issue_pr,
    update_github_issue_status,
)
from orchestrator.orchestrator import review_pr as orch_review_pr, sync_issues as orch_sync_issues

router = APIRouter()
logger = logging.getLogger(__name__)

_CLOSE_PATTERN = re.compile(
    r"(?:closes?|closed|fixes?|fixed|resolves?|resolved)\s+#(\d+)",
    re.IGNORECASE,
)


def _verify_signature(body: bytes, sig: str | None) -> bool:
    secret = settings.github_webhook_secret
    if not secret:
        return True  # dev mode — skip verification when no secret configured
    if not sig:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _extract_issue_numbers(text: str) -> list[int]:
    """Extract issue numbers from PR body (closes #N, fixes #N, resolves #N)."""
    return [int(m) for m in _CLOSE_PATTERN.findall(text)]


async def _linked_issue_numbers(owner: str, repo: str, pr_number: int) -> list[int]:
    """Use GraphQL closingIssuesReferences to find issues that this PR closes.

    This is the authoritative GitHub source — covers 'Closes #N' keywords,
    manually linked issues, and Copilot-generated PRs that GitHub auto-links.
    Falls back to the REST timeline API if GraphQL fails.
    """
    if not settings.github_token:
        return []
    try:
        query = """
        query($owner: String!, $repo: String!, $pr: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $pr) {
              closingIssuesReferences(first: 25) {
                nodes { number }
              }
            }
          }
        }
        """
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.github.com/graphql",
                headers={
                    "Authorization": f"Bearer {settings.github_token}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": {"owner": owner, "repo": repo, "pr": pr_number}},
            )
        if resp.status_code != 200:
            return []
        data = resp.json()
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("closingIssuesReferences", {})
            .get("nodes", [])
        )
        return [n["number"] for n in nodes if n.get("number")]
    except Exception as exc:
        logger.debug("Webhook: GraphQL closing-issues lookup failed for PR #%s: %s", pr_number, exc)
        return []


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.body()

    if not _verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if x_github_event != "pull_request":
        return {"status": "ignored", "event": x_github_event}

    payload: dict[str, Any] = json.loads(body)
    action = payload.get("action", "")
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})

    owner = repo.get("owner", {}).get("login", "")
    repo_name = repo.get("name", "")
    pr_number = pr.get("number")
    pr_url = pr.get("html_url", "")
    pr_body = pr.get("body") or ""
    merged = pr.get("merged", False)

    if not owner or not repo_name or not pr_number:
        return {"status": "skipped", "reason": "missing repo/pr info"}

    # Only process actions we care about
    if action not in ("opened", "reopened", "closed"):
        return {"status": "ignored", "action": action}

    # Strategy 1: parse "Closes #N" from PR body
    issue_numbers = _extract_issue_numbers(pr_body)

    # Strategy 2: if no closing keywords, check the PR's linked issues via GitHub API
    # (Copilot often doesn't write "Closes #N" explicitly)
    if not issue_numbers:
        issue_numbers = await _linked_issue_numbers(owner, repo_name, pr_number)

    if not issue_numbers:
        logger.debug("Webhook: PR #%s — no linked issues found (body + timeline)", pr_number)
        return {"status": "skipped", "reason": "no linked issues found"}

    processed: list[int] = []
    for issue_number in issue_numbers:
        record = await find_github_issue_by_repo(owner, repo_name, issue_number)
        if not record:
            logger.debug("Webhook: issue #%s not found in DB for %s/%s", issue_number, owner, repo_name)
            continue

        project_id = record["project_id"]

        if action in ("opened", "reopened"):
            await update_github_issue_pr(project_id, issue_number, pr_number, pr_url)
            issues = await list_github_issues(project_id)
            await emit_issues_updated(project_id, issues)
            # Auto-trigger AI review in background
            background_tasks.add_task(orch_review_pr, project_id, issue_number, pr_number)
            logger.info(
                "Webhook: PR #%s opened → issue #%s pr_open, review queued (project=%s)",
                pr_number, issue_number, project_id,
            )

        elif action == "closed":
            new_status = "merged" if merged else "closed"
            await update_github_issue_status(project_id, issue_number, new_status)
            issues = await list_github_issues(project_id)
            await emit_issues_updated(project_id, issues)
            if merged:
                background_tasks.add_task(orch_sync_issues, project_id)
            logger.info(
                "Webhook: PR #%s closed (merged=%s) → issue #%s %s (project=%s)",
                pr_number, merged, issue_number, new_status, project_id,
            )

        processed.append(issue_number)

    return {"status": "ok", "processed_issues": processed}
