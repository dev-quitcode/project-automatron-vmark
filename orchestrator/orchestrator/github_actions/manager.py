"""GitHub Actions environment, secrets, and workflow status management."""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from nacl.encoding import Base64Encoder
from nacl.public import PublicKey, SealedBox

from orchestrator.config import settings

logger = logging.getLogger(__name__)

# DEPRECATED: legacy SSH path; new projects use deployment_v2.kamal. Retained
# until existing projects are migrated and the legacy code is removed in a
# follow-up PR.
CI_SECRET_NAMES = {
    "host": "AUTOMATRON_DEPLOY_HOST",
    "port": "AUTOMATRON_DEPLOY_PORT",
    "user": "AUTOMATRON_DEPLOY_USER",
    "deploy_path": "AUTOMATRON_DEPLOY_PATH",
    "ssh_private_key": "AUTOMATRON_DEPLOY_SSH_PRIVATE_KEY",
    "ssh_password": "AUTOMATRON_DEPLOY_SSH_PASSWORD",
    "known_hosts": "AUTOMATRON_DEPLOY_KNOWN_HOSTS",
    "env_content": "AUTOMATRON_DEPLOY_ENV_FILE",
    "app_url": "AUTOMATRON_APP_URL",
    "health_path": "AUTOMATRON_HEALTH_PATH",
}


@dataclass
class WorkflowRunSummary:
    status: str
    run_id: str | None
    run_url: str | None
    head_sha: str | None
    created_at: str | None
    updated_at: str | None


class GitHubActionsManager:
    """Provisions GitHub Actions environments/secrets and syncs workflow runs."""

    def workflow_files(self, *, environment_name: str | None = None) -> dict[str, str]:
        environment = environment_name or settings.github_environment_name
        return {
            ".github/workflows/ci.yml": self._render_ci_workflow(),
            ".github/workflows/deploy.yml": self._render_deploy_workflow(environment),
        }

    async def ensure_environment(
        self,
        repo_name: str,
        *,
        environment_name: str | None = None,
    ) -> None:
        environment = environment_name or settings.github_environment_name
        await self._put(
            f"{self._repo_path(repo_name)}/environments/{quote(environment, safe='')}",
            json={},
        )

    async def get_environment_public_key(
        self,
        repo_name: str,
        *,
        environment_name: str | None = None,
    ) -> dict[str, str]:
        environment = environment_name or settings.github_environment_name
        return await self._get_environment_public_key(repo_name, environment)

    async def upsert_environment_secrets(
        self,
        repo_name: str,
        deploy_target: dict[str, Any],
        *,
        environment_name: str | None = None,
    ) -> list[str]:
        environment = environment_name or settings.github_environment_name
        secrets = self.build_environment_secrets(deploy_target)
        return await self.upsert_secret_pairs(
            repo_name, secrets, environment_name=environment
        )

    async def upsert_secret_pairs(
        self,
        repo_name: str,
        secrets: dict[str, str],
        *,
        environment_name: str | None = None,
    ) -> list[str]:
        """Encrypt and upsert a pre-built `name -> value` dict.

        Used by deployment_v2 paths where the strategy owns the secret schema
        and produces the dict directly. Skips empty values.
        """
        environment = environment_name or settings.github_environment_name
        non_empty = {name: value for name, value in secrets.items() if value}
        if not non_empty:
            return []
        public_key = await self._get_environment_public_key(repo_name, environment)
        for secret_name, secret_value in non_empty.items():
            encrypted_value = self._encrypt_secret(public_key["key"], secret_value)
            await self._put(
                f"{self._repo_path(repo_name)}/environments/{quote(environment, safe='')}/secrets/{secret_name}",
                json={
                    "encrypted_value": encrypted_value,
                    "key_id": public_key["key_id"],
                },
            )
        return sorted(non_empty)

    def build_environment_secrets(self, deploy_target: dict[str, Any]) -> dict[str, str]:
        required = ("host", "user", "deploy_path")
        missing = [field for field in required if not str(deploy_target.get(field, "")).strip()]
        if missing:
            raise RuntimeError(
                f"Deploy target is missing GitHub Actions secrets: {', '.join(missing)}"
            )

        auth_mode = str(deploy_target.get("auth_mode", "ssh_key") or "ssh_key").strip().lower()
        if auth_mode not in {"ssh_key", "password"}:
            raise RuntimeError(f"Unsupported deploy auth_mode: {auth_mode}")

        secrets = {
            CI_SECRET_NAMES["host"]: str(deploy_target["host"]),
            CI_SECRET_NAMES["port"]: str(deploy_target.get("port", 22) or 22),
            CI_SECRET_NAMES["user"]: str(deploy_target["user"]),
            CI_SECRET_NAMES["deploy_path"]: str(deploy_target["deploy_path"]),
        }

        if auth_mode == "password":
            ssh_password = str(deploy_target.get("ssh_password", "") or "").strip()
            if not ssh_password:
                raise RuntimeError("Deploy target is missing GitHub Actions secrets: ssh_password")
            secrets[CI_SECRET_NAMES["ssh_password"]] = ssh_password
        else:
            ssh_private_key = str(deploy_target.get("ssh_private_key", "") or "").strip()
            if not ssh_private_key:
                raise RuntimeError("Deploy target is missing GitHub Actions secrets: ssh_private_key")
            secrets[CI_SECRET_NAMES["ssh_private_key"]] = ssh_private_key

        for source_key in ("known_hosts", "env_content", "app_url", "health_path"):
            value = str(deploy_target.get(source_key, "") or "").strip()
            if value:
                secrets[CI_SECRET_NAMES[source_key]] = value

        return secrets

    async def sync_repository(
        self,
        repo_name: str,
        *,
        feature_branch: str = "",
        develop_branch: str = "develop",
        default_branch: str = "main",
    ) -> dict[str, Any]:
        runs = await self._get_workflow_runs(repo_name)

        ci_run = self._select_workflow_run(
            runs,
            settings.github_actions_ci_workflow_name,
            [feature_branch, develop_branch, default_branch],
        )
        deploy_run = self._select_workflow_run(
            runs,
            settings.github_actions_deploy_workflow_name,
            [default_branch],
        )

        return {
            "ci": self._summarize_run(ci_run, deploy=False),
            "deploy": self._summarize_run(deploy_run, deploy=True),
        }

    async def _get_environment_public_key(
        self,
        repo_name: str,
        environment_name: str,
    ) -> dict[str, str]:
        response = await self._get(
            f"{self._repo_path(repo_name)}/environments/{quote(environment_name, safe='')}/secrets/public-key"
        )
        return {
            "key": response["key"],
            "key_id": response["key_id"],
        }

    async def _get_workflow_runs(self, repo_name: str) -> list[dict[str, Any]]:
        response = await self._get(
            f"{self._repo_path(repo_name)}/actions/runs",
            params={"per_page": 50},
        )
        return response.get("workflow_runs", [])

    def _select_workflow_run(
        self,
        runs: list[dict[str, Any]],
        workflow_name: str,
        preferred_branches: list[str],
    ) -> dict[str, Any] | None:
        ordered_branches = [branch for branch in preferred_branches if branch]
        for branch in ordered_branches:
            for run in runs:
                if run.get("name") == workflow_name and run.get("head_branch") == branch:
                    return run
        for run in runs:
            if run.get("name") == workflow_name:
                return run
        return None

    def _summarize_run(
        self,
        run: dict[str, Any] | None,
        *,
        deploy: bool,
    ) -> WorkflowRunSummary:
        if run is None:
            return WorkflowRunSummary(
                status="not_configured",
                run_id=None,
                run_url=None,
                head_sha=None,
                created_at=None,
                updated_at=None,
            )

        status = self._map_run_status(run, deploy=deploy)
        return WorkflowRunSummary(
            status=status,
            run_id=str(run.get("id")) if run.get("id") is not None else None,
            run_url=run.get("html_url"),
            head_sha=run.get("head_sha"),
            created_at=run.get("created_at"),
            updated_at=run.get("updated_at"),
        )

    def _map_run_status(self, run: dict[str, Any], *, deploy: bool) -> str:
        run_status = str(run.get("status", "")).lower()
        conclusion = str(run.get("conclusion", "")).lower()

        if run_status == "completed":
            if conclusion == "success":
                return "deployed" if deploy else "succeeded"
            return "failed"
        if run_status in {"queued", "requested", "waiting"}:
            return "queued"
        if run_status in {"in_progress", "pending"}:
            return "running"
        return "queued" if deploy else "pending"

    def _encrypt_secret(self, public_key_b64: str, secret_value: str) -> str:
        public_key = PublicKey(public_key_b64.encode("utf-8"), encoder=Base64Encoder)
        sealed_box = SealedBox(public_key)
        encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
        return base64.b64encode(encrypted).decode("ascii")

    def _render_ci_workflow(self) -> str:
        return """name: CI
on:
  push:
    branches:
      - "feature/**"
      - develop

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Set up Node.js
        uses: actions/setup-node@v4
        with:
          node-version: "22"

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install Node dependencies
        if: ${{ hashFiles('package.json') != '' }}
        run: |
          if [ -f package-lock.json ]; then
            npm ci
          else
            npm install
          fi

      - name: Run Node scripts
        if: ${{ hashFiles('package.json') != '' }}
        run: |
          node - <<'NODE'
          const fs = require("fs");
          const pkg = JSON.parse(fs.readFileSync("package.json", "utf8"));
          const scripts = pkg.scripts || {};
          const commands = [];
          if (scripts.lint) commands.push("npm run lint");
          if (scripts.test) commands.push("npm run test");
          if (scripts.build) commands.push("npm run build");
          fs.writeFileSync(".automatron-node-commands", commands.join("\\n"));
          NODE
          if [ -s .automatron-node-commands ]; then
            while IFS= read -r command; do
              echo "Running ${command}"
              eval "${command}"
            done < .automatron-node-commands
          else
            echo "No Node scripts found; skipping."
          fi

      - name: Install Python dependencies
        if: ${{ hashFiles('requirements.txt') != '' }}
        run: |
          python -m pip install --upgrade pip
          python -m pip install -r requirements.txt

      - name: Run Python tests
        if: ${{ hashFiles('requirements.txt') != '' || hashFiles('pytest.ini') != '' || hashFiles('pyproject.toml') != '' }}
        run: |
          if python - <<'PY'
          import importlib.util
          raise SystemExit(0 if importlib.util.find_spec("pytest") else 1)
          PY
          then
            python -m pytest -q
          else
            echo "pytest is not installed; skipping."
          fi
"""

    def _render_deploy_workflow(self, environment_name: str) -> str:
        return f"""name: Deploy
on:
  push:
    branches:
      - main

concurrency:
  group: deploy-{environment_name}
  cancel-in-progress: true

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: {environment_name}
    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Prepare SSH
        env:
          SSH_PRIVATE_KEY: ${{{{ secrets.{CI_SECRET_NAMES["ssh_private_key"]} }}}}
          SSH_PASSWORD: ${{{{ secrets.{CI_SECRET_NAMES["ssh_password"]} }}}}
          SSH_HOST: ${{{{ secrets.{CI_SECRET_NAMES["host"]} }}}}
          SSH_PORT: ${{{{ secrets.{CI_SECRET_NAMES["port"]} }}}}
          SSH_KNOWN_HOSTS: ${{{{ secrets.{CI_SECRET_NAMES["known_hosts"]} }}}}
        run: |
          mkdir -p ~/.ssh
          if [ -n "$SSH_PRIVATE_KEY" ]; then
            printf '%s\\n' "$SSH_PRIVATE_KEY" > ~/.ssh/id_ed25519
            chmod 600 ~/.ssh/id_ed25519
          fi
          if [ -n "$SSH_KNOWN_HOSTS" ]; then
            printf '%s\\n' "$SSH_KNOWN_HOSTS" > ~/.ssh/known_hosts
          else
            ssh-keyscan -p "$SSH_PORT" "$SSH_HOST" > ~/.ssh/known_hosts
          fi
          if [ -z "$SSH_PRIVATE_KEY" ] && [ -n "$SSH_PASSWORD" ]; then
            sudo apt-get update
            sudo apt-get install -y sshpass
          fi

      - name: Build release archive
        run: |
          tar \
            --exclude=.git \
            --exclude=node_modules \
            --exclude=.next \
            --exclude=.venv \
            --exclude=__pycache__ \
            -czf /tmp/automatron-release.tgz .

      - name: Upload release
        env:
          SSH_PRIVATE_KEY: ${{{{ secrets.{CI_SECRET_NAMES["ssh_private_key"]} }}}}
          SSH_PASSWORD: ${{{{ secrets.{CI_SECRET_NAMES["ssh_password"]} }}}}
          SSH_HOST: ${{{{ secrets.{CI_SECRET_NAMES["host"]} }}}}
          SSH_PORT: ${{{{ secrets.{CI_SECRET_NAMES["port"]} }}}}
          SSH_USER: ${{{{ secrets.{CI_SECRET_NAMES["user"]} }}}}
          DEPLOY_PATH: ${{{{ secrets.{CI_SECRET_NAMES["deploy_path"]} }}}}
          DEPLOY_ENV_FILE: ${{{{ secrets.{CI_SECRET_NAMES["env_content"]} }}}}
        run: |
          if [ -n "$SSH_PRIVATE_KEY" ]; then
            ssh -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" "mkdir -p '$DEPLOY_PATH'"
            scp -P "$SSH_PORT" /tmp/automatron-release.tgz "$SSH_USER@$SSH_HOST:/tmp/automatron-release.tgz"
          else
            export SSHPASS="$SSH_PASSWORD"
            sshpass -e ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" "mkdir -p '$DEPLOY_PATH'"
            sshpass -e scp -o PreferredAuthentications=password -o PubkeyAuthentication=no -P "$SSH_PORT" /tmp/automatron-release.tgz "$SSH_USER@$SSH_HOST:/tmp/automatron-release.tgz"
          fi
          if [ -n "$DEPLOY_ENV_FILE" ]; then
            printf '%s' "$DEPLOY_ENV_FILE" > /tmp/automatron.env
            if [ -n "$SSH_PRIVATE_KEY" ]; then
              scp -P "$SSH_PORT" /tmp/automatron.env "$SSH_USER@$SSH_HOST:$DEPLOY_PATH/.env"
            else
              export SSHPASS="$SSH_PASSWORD"
              sshpass -e scp -o PreferredAuthentications=password -o PubkeyAuthentication=no -P "$SSH_PORT" /tmp/automatron.env "$SSH_USER@$SSH_HOST:$DEPLOY_PATH/.env"
            fi
          fi

      - name: Deploy on target
        env:
          SSH_PRIVATE_KEY: ${{{{ secrets.{CI_SECRET_NAMES["ssh_private_key"]} }}}}
          SSH_PASSWORD: ${{{{ secrets.{CI_SECRET_NAMES["ssh_password"]} }}}}
          SSH_HOST: ${{{{ secrets.{CI_SECRET_NAMES["host"]} }}}}
          SSH_PORT: ${{{{ secrets.{CI_SECRET_NAMES["port"]} }}}}
          SSH_USER: ${{{{ secrets.{CI_SECRET_NAMES["user"]} }}}}
          DEPLOY_PATH: ${{{{ secrets.{CI_SECRET_NAMES["deploy_path"]} }}}}
        run: |
          if [ -n "$SSH_PRIVATE_KEY" ]; then
            ssh -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" "
              set -e
              mkdir -p '$DEPLOY_PATH'
              tar -xzf /tmp/automatron-release.tgz -C '$DEPLOY_PATH'
              rm -f /tmp/automatron-release.tgz
              cd '$DEPLOY_PATH'
              docker compose -f deploy/docker-compose.yml up -d --build
            "
          else
            export SSHPASS="$SSH_PASSWORD"
            sshpass -e ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" "
              set -e
              mkdir -p '$DEPLOY_PATH'
              tar -xzf /tmp/automatron-release.tgz -C '$DEPLOY_PATH'
              rm -f /tmp/automatron-release.tgz
              cd '$DEPLOY_PATH'
              docker compose -f deploy/docker-compose.yml up -d --build
            "
          fi

      - name: Health check
        env:
          APP_URL: ${{{{ secrets.{CI_SECRET_NAMES["app_url"]} }}}}
          HEALTH_PATH: ${{{{ secrets.{CI_SECRET_NAMES["health_path"]} }}}}
        run: |
          if [ -z "$APP_URL" ]; then
            echo "No health check configured."
            exit 0
          fi
          URL="${{APP_URL%/}}"
          if [ -n "$HEALTH_PATH" ]; then
            case "$HEALTH_PATH" in
              /*) URL="${{URL}}${{HEALTH_PATH}}" ;;
              *) URL="${{URL}}/${{HEALTH_PATH}}" ;;
            esac
          fi
          curl --fail --retry 5 --retry-delay 5 "$URL"
"""

    # ── deployment_v2 workflow dispatch / run correlation ─────────────────────

    async def dispatch_workflow(
        self,
        repo_name: str,
        workflow_filename: str,
        *,
        ref: str = "main",
        inputs: dict[str, str] | None = None,
    ) -> None:
        """POST /repos/{owner}/{repo}/actions/workflows/{file}/dispatches.

        GitHub does not return the run id from this endpoint — callers must
        correlate via `_match_run_by_correlation` using the
        `automatron_run_id` input that the workflow echoes in `run-name`.
        """
        body: dict[str, Any] = {"ref": ref}
        if inputs:
            body["inputs"] = inputs
        await self._post(
            f"{self._repo_path(repo_name)}/actions/workflows/"
            f"{quote(workflow_filename, safe='')}/dispatches",
            json=body,
        )

    async def match_run_by_correlation(
        self,
        repo_name: str,
        workflow_filename: str,
        automatron_run_id: str,
        *,
        max_retries: int = 10,
        delay_s: float = 2.0,
    ) -> WorkflowRunSummary:
        """Poll workflow runs until one whose `run-name` matches the id appears."""
        if not automatron_run_id:
            raise ValueError("automatron_run_id is required for correlation")

        path = (
            f"{self._repo_path(repo_name)}/actions/workflows/"
            f"{quote(workflow_filename, safe='')}/runs"
        )
        for attempt in range(max_retries):
            response = await self._get(
                path, params={"event": "workflow_dispatch", "per_page": 20}
            )
            for run in response.get("workflow_runs", []):
                run_name = str(run.get("name") or run.get("display_title") or "")
                if automatron_run_id in run_name:
                    return self._summarize_run(run, deploy=True)
            if attempt + 1 < max_retries:
                await asyncio.sleep(delay_s)
        return WorkflowRunSummary(
            status="not_configured",
            run_id=None,
            run_url=None,
            head_sha=None,
            created_at=None,
            updated_at=None,
        )

    async def get_workflow_run(
        self,
        repo_name: str,
        run_id: str,
    ) -> WorkflowRunSummary:
        """GET /repos/{owner}/{repo}/actions/runs/{run_id}."""
        if not run_id:
            raise ValueError("run_id is required")
        run = await self._get(f"{self._repo_path(repo_name)}/actions/runs/{run_id}")
        return self._summarize_run(run, deploy=True)

    async def download_workflow_logs(self, repo_name: str, run_id: str) -> bytes:
        """GET /repos/{owner}/{repo}/actions/runs/{run_id}/logs (zip)."""
        if not run_id:
            raise ValueError("run_id is required")
        async with httpx.AsyncClient(
            base_url=settings.github_api_url,
            headers=self._headers(),
            timeout=60,
            follow_redirects=True,
        ) as client:
            response = await client.get(
                f"{self._repo_path(repo_name)}/actions/runs/{run_id}/logs"
            )
            response.raise_for_status()
            return response.content

    async def _post(self, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=settings.github_api_url,
            headers=self._headers(),
            timeout=30,
        ) as client:
            response = await client.post(path, json=json)
            if response.status_code not in {200, 201, 204}:
                response.raise_for_status()
            return response.json() if response.content else {}

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=settings.github_api_url,
            headers=self._headers(),
            timeout=30,
        ) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()

    async def _put(self, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=settings.github_api_url,
            headers=self._headers(),
            timeout=30,
        ) as client:
            response = await client.put(path, json=json)
            response.raise_for_status()
            return response.json() if response.content else {}

    def _headers(self) -> dict[str, str]:
        if not settings.github_owner:
            raise RuntimeError("GITHUB_OWNER is required for GitHub Actions management")
        if not settings.github_token:
            raise RuntimeError("GITHUB_TOKEN is required for GitHub Actions management")
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {settings.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _repo_path(self, repo_name: str) -> str:
        repo = (repo_name or "").strip()
        if not repo:
            raise RuntimeError("repo_name is required for GitHub Actions management")
        if "/" in repo:
            owner, name = repo.split("/", 1)
            owner = owner.strip()
            name = name.strip()
            if not owner or not name:
                raise RuntimeError(f"Invalid repo_name format: {repo_name!r}")
            return f"/repos/{owner}/{name}"
        if not settings.github_owner:
            raise RuntimeError(
                "GITHUB_OWNER is required when repo_name does not include owner"
            )
        return f"/repos/{settings.github_owner}/{repo}"
