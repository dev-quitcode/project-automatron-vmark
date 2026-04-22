"""GitHub Issues, Milestones, and Pull Requests API client."""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from orchestrator.config import settings

logger = logging.getLogger(__name__)


class GitHubClient:
    """Async GitHub REST API client for issues, milestones, PRs, and file ops."""

    def __init__(self) -> None:
        self._base_url = settings.github_api_url or "https://api.github.com"
        self._token = settings.github_token

    # ── Low-level ────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _client(self, timeout: float = 20.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers(),
            timeout=timeout,
        )

    # ── Repository files ─────────────────────────────────────────────────────

    async def read_file(self, owner: str, repo: str, path: str) -> str | None:
        """Return decoded file content, or None if the file does not exist."""
        async with self._client() as client:
            response = await client.get(f"/repos/{owner}/{repo}/contents/{path}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return None  # path is a directory
            encoded = data.get("content", "")
            return base64.b64decode(encoded).decode("utf-8")

    async def push_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        commit_message: str,
        branch: str = "main",
    ) -> None:
        """Create or update a file in the repo via the Contents API."""
        # Check if the file already exists (need its SHA to update)
        sha: str | None = None
        async with self._client() as client:
            check = await client.get(f"/repos/{owner}/{repo}/contents/{path}")
            if check.status_code == 200:
                sha = check.json().get("sha")

        payload: dict[str, Any] = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        async with self._client() as client:
            response = await client.put(f"/repos/{owner}/{repo}/contents/{path}", json=payload)
            if response.status_code not in {200, 201}:
                raise RuntimeError(
                    f"Failed to push {path} to {owner}/{repo}: "
                    f"{response.status_code} {response.text}"
                )

    # ── Milestones ────────────────────────────────────────────────────────────

    async def create_milestone(
        self, owner: str, repo: str, title: str, description: str = ""
    ) -> int:
        """Create a milestone and return its number."""
        async with self._client() as client:
            response = await client.post(
                f"/repos/{owner}/{repo}/milestones",
                json={"title": title, "description": description, "state": "open"},
            )
            if response.status_code == 422:
                # Already exists — find it
                existing = await self.list_milestones(owner, repo)
                for m in existing:
                    if m["title"] == title:
                        return m["number"]
                raise RuntimeError(f"Milestone '{title}' returned 422 but was not found")
            response.raise_for_status()
            return response.json()["number"]

    async def list_milestones(self, owner: str, repo: str) -> list[dict[str, Any]]:
        async with self._client() as client:
            response = await client.get(
                f"/repos/{owner}/{repo}/milestones",
                params={"state": "all", "per_page": 100},
            )
            response.raise_for_status()
            return response.json()

    # ── Labels ────────────────────────────────────────────────────────────────

    async def ensure_label(
        self, owner: str, repo: str, name: str, color: str = "ededed"
    ) -> None:
        async with self._client() as client:
            r = await client.post(
                f"/repos/{owner}/{repo}/labels",
                json={"name": name, "color": color},
            )
            if r.status_code not in {201, 422}:
                r.raise_for_status()

    # ── Issues ────────────────────────────────────────────────────────────────

    async def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        milestone_number: int | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if milestone_number:
            payload["milestone"] = milestone_number
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees

        async with self._client(timeout=30.0) as client:
            response = await client.post(f"/repos/{owner}/{repo}/issues", json=payload)
            response.raise_for_status()
            return response.json()

    async def trigger_copilot_agent(
        self, owner: str, repo: str, issue_number: int
    ) -> None:
        """Assign copilot-swe-agent[bot] to the issue to trigger the Copilot coding agent."""
        async with self._client(timeout=15.0) as client:
            response = await client.post(
                f"/repos/{owner}/{repo}/issues/{issue_number}/assignees",
                json={
                    "assignees": ["copilot-swe-agent[bot]"],
                    "agent_assignment": {
                        "target_repo": f"{owner}/{repo}",
                        "base_branch": "main",
                    },
                },
            )
            response.raise_for_status()

    async def list_issues(
        self,
        owner: str,
        repo: str,
        milestone: str | int | None = None,
        state: str = "all",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"state": state, "per_page": 100}
        if milestone is not None:
            params["milestone"] = str(milestone)

        async with self._client() as client:
            response = await client.get(f"/repos/{owner}/{repo}/issues", params=params)
            response.raise_for_status()
            # Filter out pull requests (GitHub returns PRs in issues endpoint)
            return [i for i in response.json() if "pull_request" not in i]

    async def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.get(f"/repos/{owner}/{repo}/issues/{number}")
            response.raise_for_status()
            return response.json()

    # ── Pull Requests ─────────────────────────────────────────────────────────

    async def list_prs(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        async with self._client() as client:
            response = await client.get(
                f"/repos/{owner}/{repo}/pulls",
                params={"state": state, "per_page": 100},
            )
            response.raise_for_status()
            return response.json()

    async def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return the unified diff of a PR."""
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={**self._headers(), "Accept": "application/vnd.github.diff"},
            timeout=30.0,
        ) as client:
            response = await client.get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
            response.raise_for_status()
            return response.text

    async def post_pr_comment(
        self, owner: str, repo: str, pr_number: int, body: str
    ) -> None:
        """Post a review comment on a PR (as a regular issue comment)."""
        async with self._client() as client:
            response = await client.post(
                f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
                json={"body": body},
            )
            response.raise_for_status()

    async def find_pr_for_issue(
        self, owner: str, repo: str, issue_number: int
    ) -> dict[str, Any] | None:
        """Find a PR linked to the given issue.

        Strategy:
        1. Check issue timeline for cross-referenced PRs (works even when Copilot
           doesn't write 'Closes #N' in the PR body).
        2. Fall back to scanning PR bodies for '#N' mentions.
        """
        async with self._client(timeout=20.0) as client:
            # Strategy 1 — issue timeline (most reliable)
            resp = await client.get(
                f"/repos/{owner}/{repo}/issues/{issue_number}/timeline",
                headers={"Accept": "application/vnd.github.mockingbird-preview+json"},
                params={"per_page": 100},
            )
            if resp.status_code == 200:
                for event in resp.json():
                    if event.get("event") == "cross-referenced":
                        source = event.get("source", {})
                        issue_ref = source.get("issue", {})
                        pr_url = issue_ref.get("pull_request", {}).get("url", "")
                        if pr_url:
                            pr_resp = await client.get(pr_url)
                            if pr_resp.status_code == 200:
                                return pr_resp.json()

        # Strategy 2 — scan PR bodies for '#N' mention
        for state in ("open", "closed"):
            prs = await self.list_prs(owner, repo, state=state)
            for pr in prs:
                body = (pr.get("body") or "").lower()
                for keyword in (f"closes #{issue_number}", f"fixes #{issue_number}",
                                f"resolves #{issue_number}", f"#{issue_number}"):
                    if keyword in body:
                        return pr
        return None

    # ── Webhooks ─────────────────────────────────────────────────────────────

    async def register_webhook(self, owner: str, repo: str) -> str:
        """Idempotently register the Automatron webhook on a GitHub repo.

        Returns: "registered" | "already_exists" | "skipped" | "error: <msg>"
        Requires AUTOMATRON_PUBLIC_URL and optionally GITHUB_WEBHOOK_SECRET in config.
        """
        public_url = settings.automatron_public_url.rstrip("/")
        if not public_url:
            return "skipped"

        webhook_url = f"{public_url}/api/webhooks/github"

        async with self._client(timeout=15) as client:
            # Check for an existing hook to avoid duplicates
            resp = await client.get(f"/repos/{owner}/{repo}/hooks")
            if resp.status_code == 200:
                for hook in resp.json():
                    if hook.get("config", {}).get("url") == webhook_url:
                        return "already_exists"
            elif resp.status_code not in {200, 404}:
                return f"error: list hooks {resp.status_code}"

            payload: dict[str, Any] = {
                "name": "web",
                "active": True,
                "events": ["pull_request"],
                "config": {
                    "url": webhook_url,
                    "content_type": "json",
                    "insecure_ssl": "0",
                },
            }
            if settings.github_webhook_secret:
                payload["config"]["secret"] = settings.github_webhook_secret

            create_resp = await client.post(f"/repos/{owner}/{repo}/hooks", json=payload)
            if create_resp.status_code == 201:
                return "registered"
            return f"error: {create_resp.status_code} {create_resp.text[:200]}"

    # ── Deployments ──────────────────────────────────────────────────────────

    async def get_preview_url_from_deployments(self, owner: str, repo: str) -> str | None:
        """Check GitHub Deployments for a live environment URL.

        Checks environments in priority order: production → preview → staging → any.
        Returns the first successful deployment's environment_url, or None.
        """
        async with self._client(timeout=15) as client:
            resp = await client.get(
                f"/repos/{owner}/{repo}/deployments",
                params={"per_page": 20},
            )
            if resp.status_code != 200:
                return None

            deployments = resp.json()
            if not deployments:
                return None

            # Sort by priority: production first, then preview/staging, then any
            def env_priority(d: dict) -> int:
                env = (d.get("environment") or "").lower()
                if env == "production":
                    return 0
                if env in ("preview", "staging"):
                    return 1
                return 2

            deployments.sort(key=env_priority)

            for deployment in deployments:
                dep_id = deployment["id"]
                statuses_resp = await client.get(
                    f"/repos/{owner}/{repo}/deployments/{dep_id}/statuses",
                    params={"per_page": 1},
                )
                if statuses_resp.status_code != 200:
                    continue
                statuses = statuses_resp.json()
                if not statuses:
                    continue
                latest = statuses[0]
                if latest.get("state") == "success":
                    env_url = latest.get("environment_url") or latest.get("target_url") or ""
                    if env_url:
                        return env_url

        return None

