"""Application configuration via Pydantic Settings."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Global application settings loaded from environment variables."""

    # --- LLM Providers ---
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""
    github_token: str = ""
    github_webhook_secret: str = ""
    automatron_public_url: str = ""  # e.g. https://automatron.example.com — used for auto-registering webhooks
    figma_access_token: str = ""    # Figma personal access token for reading design context
    github_owner: str = ""
    github_owner_type: str = "user"
    github_default_org: str = ""
    github_api_url: str = "https://api.github.com"
    github_repo_visibility: str = "private"
    github_environment_name: str = "production"
    github_actions_ci_workflow_name: str = "CI"
    github_actions_deploy_workflow_name: str = "Deploy"
    git_author_name: str = "Automatron Bot"
    git_author_email: str = "automatron@example.local"

    # --- Architect ---
    architect_model: str = "gpt-5.3-codex"
    architect_prompt_version: str = "v1"

    # --- Builder ---
    builder_model: str = "gpt-5.3-codex"
    builder_cline_timeout: int = 900
    reviewer_model: str = "gpt-5.3-codex"

    # --- Docker ---
    golden_image: str = "automatron/golden:latest"
    workspace_base_path: str = "/var/automatron/workspaces"
    port_range_start: int = 7000
    port_range_end: int = 7999

    # --- Deploy ---
    deploy_ssh_key_path: str = ""
    deploy_ssh_options: str = ""

    # --- Database ---
    sqlite_db_path: str = "./data/automatron.db"
    checkpoint_db_path: str = "./data/checkpoints.db"

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # --- Auth (Google OAuth via Auth.js v5) ---
    # AUTH_SECRET is shared with the web-ui's NextAuth config. Used to verify the
    # session JWT cookie. Generate with: openssl rand -base64 32
    auth_secret: str = ""
    # Comma-separated allowlist of email addresses that can sign in. Leave empty
    # to disable auth entirely (dev mode / pre-OAuth deployments).
    automatron_allowed_emails: str = ""
    # Local-dev escape hatch: when true, require_auth always returns a fake user.
    # NEVER set in production.
    automatron_dev_no_auth: bool = False

    @field_validator("debug", mode="before")
    @classmethod
    def _parse_debug_flag(cls, value: object) -> object:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on", "debug", "dev", "development"}:
                return True
            if normalized in {"0", "false", "no", "off", "release", "prod", "production"}:
                return False
        return value

    @property
    def sqlite_db_dir(self) -> Path:
        path = Path(self.sqlite_db_path).parent
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def _project_root(cls) -> Path:
        return Path(__file__).resolve().parents[1]

    @field_validator("sqlite_db_path", "checkpoint_db_path", mode="before")
    @classmethod
    def _normalize_sqlite_paths(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            return value
        path = Path(raw)
        if path.is_absolute():
            path.parent.mkdir(parents=True, exist_ok=True)
            return str(path)
        normalized = (cls._project_root() / path).resolve()
        normalized.parent.mkdir(parents=True, exist_ok=True)
        return str(normalized)

    @property
    def workspace_base_dir(self) -> Path:
        raw_path = self.workspace_base_path.strip()
        if os.name == "nt" and raw_path.startswith("/"):
            path = Path.cwd() / "workspaces"
        else:
            path = Path(raw_path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
