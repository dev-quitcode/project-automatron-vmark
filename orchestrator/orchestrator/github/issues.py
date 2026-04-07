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
        """Search open + closed PRs to find one referencing the given issue number."""
        for state in ("open", "closed"):
            prs = await self.list_prs(owner, repo, state=state)
            for pr in prs:
                body = (pr.get("body") or "").lower()
                # GitHub auto-links "closes #N", "fixes #N", "resolves #N"
                for keyword in (f"closes #{issue_number}", f"fixes #{issue_number}",
                                f"resolves #{issue_number}", f"#{issue_number}"):
                    if keyword in body:
                        return pr
        return None

