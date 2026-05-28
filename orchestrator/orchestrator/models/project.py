"""Project SQLite models and CRUD operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from orchestrator.config import settings
from orchestrator.llm.configuration import default_llm_config, normalize_llm_config

logger = logging.getLogger(__name__)

_db_path: str = ""

PROJECT_COLUMN_DEFS: dict[str, str] = {
    "project_stage": "TEXT NOT NULL DEFAULT 'intake'",
    "intake_text": "TEXT NOT NULL DEFAULT ''",
    "intake_source": "TEXT NOT NULL DEFAULT 'manual'",
    "source_ref": "TEXT",
    "llm_config_json": "TEXT",
    "execution_contract_json": "TEXT",
    "decision_log_json": "TEXT",
    "plan_delta_history_json": "TEXT",
    "task_validation_result_json": "TEXT",
    "last_escalation_json": "TEXT",
    "builder_report_json": "TEXT",
    "repo_name": "TEXT",
    "repo_url": "TEXT",
    "repo_clone_url": "TEXT",
    "default_branch": "TEXT",
    "develop_branch": "TEXT",
    "feature_branch": "TEXT",
    "repo_ready": "INTEGER NOT NULL DEFAULT 0",
    "contract_version": "INTEGER NOT NULL DEFAULT 0",
    "active_task_id": "TEXT",
    "task_attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "preview_url": "TEXT",
    "preview_status": "TEXT NOT NULL DEFAULT 'pending'",
    "preview_checked_at": "TEXT",
    "preview_metadata_json": "TEXT",
    "ci_status": "TEXT NOT NULL DEFAULT 'not_configured'",
    "ci_run_id": "TEXT",
    "ci_run_url": "TEXT",
    "deploy_status": "TEXT NOT NULL DEFAULT 'not_configured'",
    "deploy_run_url": "TEXT",
    "deploy_commit_sha": "TEXT",
    "deploy_target_json": "TEXT",
    "github_environment_name": "TEXT NOT NULL DEFAULT 'production'",
    "last_workflow_sync_at": "TEXT",
    "plan_approved": "INTEGER NOT NULL DEFAULT 0",
    "preview_approved": "INTEGER NOT NULL DEFAULT 0",
    "plan_approved_at": "TEXT",
    "preview_approved_at": "TEXT",
    "approval_history_json": "TEXT",
    "last_deploy_at": "TEXT",
    "last_deploy_run_id": "TEXT",
    "github_repo_owner": "TEXT",
    "github_repo_name": "TEXT",
    "issue_plan_json": "TEXT",
    "figma_urls_json": "TEXT NOT NULL DEFAULT '[]'",
    "figma_file_context": "TEXT NOT NULL DEFAULT ''",
    "supabase_url": "TEXT",
    "supabase_service_role_key": "TEXT",
    "supabase_anon_key": "TEXT",
}

JSON_FIELDS = {
    "stack_config_json",
    "llm_config_json",
    "execution_contract_json",
    "decision_log_json",
    "plan_delta_history_json",
    "task_validation_result_json",
    "last_escalation_json",
    "builder_report_json",
    "deploy_target_json",
    "preview_metadata_json",
    "approval_history_json",
    "issue_plan_json",
    "figma_urls_json",
}
BOOL_FIELDS = {"repo_ready", "plan_approved", "preview_approved"}
JSON_FIELD_DEFAULTS: dict[str, Any] = {
    "stack_config_json": {},
    "llm_config_json": default_llm_config(),
    "execution_contract_json": {},
    "decision_log_json": [],
    "plan_delta_history_json": [],
    "task_validation_result_json": {},
    "last_escalation_json": {},
    "builder_report_json": {},
    "deploy_target_json": {},
    "preview_metadata_json": {},
    "approval_history_json": [],
    "issue_plan_json": {},
    "figma_urls_json": [],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ensure_db_ready() -> None:
    target_path = _db_path or settings.sqlite_db_path
    if not target_path:
        raise RuntimeError("SQLite database path is not configured")
    if not _db_path or not Path(target_path).exists():
        await init_db(target_path)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _serialize_project_row(row: aiosqlite.Row) -> dict[str, Any]:
    project = dict(row)

    for field in JSON_FIELDS:
        project[field] = _json_loads(project.get(field), JSON_FIELD_DEFAULTS[field])

    for field in BOOL_FIELDS:
        project[field] = bool(project.get(field))

    deploy_target = project.get("deploy_target_json") or {}
    project["deploy_target"] = deploy_target
    project["deploy_target_summary"] = _summarize_deploy_target(deploy_target)
    project["stack_config"] = project.get("stack_config_json") or {}
    project["llm_config"] = normalize_llm_config(project.get("llm_config_json") or {})
    project["execution_contract"] = project.get("execution_contract_json") or {}
    project["decision_log"] = project.get("decision_log_json") or []
    project["plan_delta_history"] = project.get("plan_delta_history_json") or []
    project["task_validation_result"] = project.get("task_validation_result_json") or {}
    project["last_escalation"] = project.get("last_escalation_json") or {}
    project["builder_report"] = project.get("builder_report_json") or {}
    project["preview_metadata"] = project.get("preview_metadata_json") or {}
    project["approval_history"] = project.get("approval_history_json") or []
    project["description"] = project.get("intake_text", "")
    project["preview_port"] = project.get("port")

    return project


def _summarize_deploy_target(target: dict[str, Any] | None) -> dict[str, Any] | None:
    if not target:
        return None

    return {
        "auth_mode": target.get("auth_mode", "ssh_key"),
        "host": target.get("host"),
        "port": target.get("port", 22),
        "user": target.get("user"),
        "deploy_path": target.get("deploy_path"),
        "auth_reference": target.get("auth_reference"),
        "app_url": target.get("app_url"),
        "health_path": target.get("health_path") or "/api/health",
    }


async def _ensure_columns(
    db: aiosqlite.Connection,
    table_name: str,
    columns: dict[str, str],
) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    existing = {row[1] for row in await cursor.fetchall()}
    for column, definition in columns.items():
        if column not in existing:
            await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}")


async def init_db(db_path: str) -> None:
    """Initialize the project database, creating tables if needed."""
    global _db_path
    _db_path = db_path

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                project_stage TEXT NOT NULL DEFAULT 'intake',
                intake_text TEXT NOT NULL DEFAULT '',
                intake_source TEXT NOT NULL DEFAULT 'manual',
                source_ref TEXT,
                plan_md TEXT,
                stack_config_json TEXT,
                llm_config_json TEXT,
                execution_contract_json TEXT,
                decision_log_json TEXT,
                plan_delta_history_json TEXT,
                task_validation_result_json TEXT,
                last_escalation_json TEXT,
                builder_report_json TEXT,
                repo_name TEXT,
                repo_url TEXT,
                repo_clone_url TEXT,
                default_branch TEXT,
                develop_branch TEXT,
                feature_branch TEXT,
                repo_ready INTEGER NOT NULL DEFAULT 0,
                contract_version INTEGER NOT NULL DEFAULT 0,
                active_task_id TEXT,
                task_attempt_count INTEGER NOT NULL DEFAULT 0,
                container_id TEXT,
                port INTEGER,
                preview_url TEXT,
                preview_status TEXT NOT NULL DEFAULT 'pending',
                preview_checked_at TEXT,
                preview_metadata_json TEXT,
                ci_status TEXT NOT NULL DEFAULT 'not_configured',
                ci_run_id TEXT,
                ci_run_url TEXT,
                deploy_status TEXT NOT NULL DEFAULT 'not_configured',
                deploy_run_url TEXT,
                deploy_commit_sha TEXT,
                deploy_target_json TEXT,
                github_environment_name TEXT NOT NULL DEFAULT 'production',
                last_workflow_sync_at TEXT,
                plan_approved INTEGER NOT NULL DEFAULT 0,
                preview_approved INTEGER NOT NULL DEFAULT 0,
                plan_approved_at TEXT,
                preview_approved_at TEXT,
                approval_history_json TEXT,
                last_deploy_at TEXT,
                last_deploy_run_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS task_logs (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                task_index INTEGER NOT NULL,
                task_text TEXT,
                status TEXT NOT NULL,
                cline_output TEXT,
                duration_s REAL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS deploy_runs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                status TEXT NOT NULL,
                branch TEXT NOT NULL,
                output TEXT,
                summary_json TEXT,
                created_at TEXT NOT NULL,
                deployed_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS trace_events (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                session_id TEXT,
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                stage TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_logs (
                id          TEXT PRIMARY KEY,
                project_id  TEXT NOT NULL,
                seq         INTEGER NOT NULL,
                task_text   TEXT NOT NULL,
                output      TEXT,
                status      TEXT NOT NULL DEFAULT 'INFO',
                created_at  TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS github_issues (
                id          TEXT PRIMARY KEY,
                project_id  TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                title       TEXT NOT NULL,
                epic        TEXT,
                story       TEXT,
                status      TEXT NOT NULL DEFAULT 'open',
                pr_number   INTEGER,
                pr_url      TEXT,
                pr_review_json TEXT,
                copilot_workspace_url TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                delivery_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL
            )
            """
        )

        await _ensure_columns(db, "projects", PROJECT_COLUMN_DEFS)
        await _ensure_columns(db, "github_issues", {
            "build_status": "TEXT",
            "implementing_started_at": "TEXT",
        })
        await db.commit()
        logger.info("Database initialized: %s", db_path)

        # Garbage-collect webhook delivery records older than 24h
        await db.execute(
            "DELETE FROM webhook_deliveries WHERE received_at < ?",
            ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),),
        )
        await db.commit()

        # Sweep any stale "implementing" issues that got orphaned by an orchestrator
        # crash / restart. >30 min in `implementing` is the threshold — real Aider runs
        # take at most a few minutes; anything older is dead.
        sweep_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        cur = await db.execute(
            """
            UPDATE github_issues
            SET status = 'open', updated_at = ?
            WHERE status = 'implementing'
              AND (implementing_started_at IS NULL OR implementing_started_at < ?)
            """,
            (_now(), sweep_cutoff),
        )
        await db.commit()
        if cur.rowcount:
            logger.warning(
                "Database init: swept %d stale 'implementing' issue(s) back to 'open'",
                cur.rowcount,
            )


async def create_project(
    project_id: str,
    name: str,
    intake_text: str,
    intake_source: str = "manual",
    source_ref: str | None = None,
    llm_config: dict[str, Any] | None = None,
    github_repo_owner: str | None = None,
    github_repo_name: str | None = None,
    figma_urls: list[str] | None = None,
    supabase_url: str | None = None,
    supabase_service_role_key: str | None = None,
    supabase_anon_key: str | None = None,
) -> dict[str, Any]:
    """Create a new project record."""
    await _ensure_db_ready()
    now = _now()
    normalized_llm_config = normalize_llm_config(llm_config)
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            """
            INSERT INTO projects (
                id,
                name,
                status,
                project_stage,
                intake_text,
                intake_source,
                source_ref,
                plan_md,
                stack_config_json,
                llm_config_json,
                preview_status,
                ci_status,
                deploy_status,
                github_environment_name,
                approval_history_json,
                github_repo_owner,
                github_repo_name,
                figma_urls_json,
                supabase_url,
                supabase_service_role_key,
                supabase_anon_key,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'pending', 'intake', ?, ?, ?, '', '{}', ?, 'pending', 'not_configured', 'not_configured', ?, '[]', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                name,
                intake_text,
                intake_source,
                source_ref,
                _json_dumps(normalized_llm_config),
                settings.github_environment_name,
                github_repo_owner,
                github_repo_name,
                _json_dumps(figma_urls or []),
                supabase_url,
                supabase_service_role_key,
                supabase_anon_key,
                now,
                now,
            ),
        )
        await db.commit()

    project = await get_project(project_id)
    if not project:
        raise RuntimeError(f"Failed to create project {project_id}")
    return project


async def get_project(project_id: str) -> dict[str, Any] | None:
    """Get a project by ID."""
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = await cursor.fetchone()
        return _serialize_project_row(row) if row else None


async def get_all_projects() -> list[dict[str, Any]]:
    """Get all projects ordered by creation date."""
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM projects ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [_serialize_project_row(row) for row in rows]


def _normalize_update_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(kwargs)

    if "stack_config" in normalized:
        normalized["stack_config_json"] = _json_dumps(normalized.pop("stack_config"))
    if "llm_config" in normalized:
        normalized["llm_config_json"] = _json_dumps(normalize_llm_config(normalized.pop("llm_config")))
    if "execution_contract" in normalized:
        normalized["execution_contract_json"] = _json_dumps(normalized.pop("execution_contract"))
    if "decision_log" in normalized:
        normalized["decision_log_json"] = _json_dumps(normalized.pop("decision_log"))
    if "plan_delta_history" in normalized:
        normalized["plan_delta_history_json"] = _json_dumps(normalized.pop("plan_delta_history"))
    if "task_validation_result" in normalized:
        normalized["task_validation_result_json"] = _json_dumps(normalized.pop("task_validation_result"))
    if "last_escalation" in normalized:
        normalized["last_escalation_json"] = _json_dumps(normalized.pop("last_escalation"))
    if "builder_report" in normalized:
        normalized["builder_report_json"] = _json_dumps(normalized.pop("builder_report"))
    if "deploy_target" in normalized:
        normalized["deploy_target_json"] = _json_dumps(normalized.pop("deploy_target"))
    if "preview_metadata" in normalized:
        normalized["preview_metadata_json"] = _json_dumps(normalized.pop("preview_metadata"))
    if "approval_history" in normalized:
        normalized["approval_history_json"] = _json_dumps(normalized.pop("approval_history"))

    for field in JSON_FIELDS:
        if field in normalized and isinstance(normalized[field], (dict, list)):
            normalized[field] = _json_dumps(normalized[field])

    for field in BOOL_FIELDS:
        if field in normalized:
            normalized[field] = 1 if normalized[field] else 0

    return normalized


async def update_project(project_id: str, **kwargs: Any) -> None:
    """Update project fields."""
    if not kwargs:
        return

    await _ensure_db_ready()
    normalized = _normalize_update_kwargs(kwargs)
    normalized["updated_at"] = _now()

    set_clause = ", ".join(f"{key} = ?" for key in normalized)
    values = list(normalized.values()) + [project_id]

    async with aiosqlite.connect(_db_path) as db:
        await db.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", values)
        await db.commit()


async def update_project_plan(project_id: str, plan_md: str) -> None:
    await update_project(project_id, plan_md=plan_md)


async def update_project_llm_config(project_id: str, llm_config: dict[str, Any]) -> None:
    await update_project(project_id, llm_config=normalize_llm_config(llm_config))


async def update_project_status(project_id: str, status: str) -> None:
    await update_project(project_id, status=status)


async def update_project_stage(project_id: str, project_stage: str) -> None:
    await update_project(project_id, project_stage=project_stage)


async def update_project_container(project_id: str, container_id: str, port: int) -> None:
    await update_project(project_id, container_id=container_id, port=port)


async def update_project_repo(project_id: str, **repo_fields: Any) -> None:
    await update_project(project_id, **repo_fields)


async def update_project_preview(
    project_id: str,
    preview_url: str | None,
    preview_status: str,
    preview_metadata: dict[str, Any] | None = None,
) -> None:
    await update_project(
        project_id,
        preview_url=preview_url,
        preview_status=preview_status,
        preview_metadata=preview_metadata or {},
        preview_checked_at=_now(),
    )


async def update_project_deploy_target(project_id: str, deploy_target: dict[str, Any]) -> None:
    await update_project(project_id, deploy_target=deploy_target, deploy_status="configured")


async def update_project_cicd(
    project_id: str,
    *,
    ci_status: str | None = None,
    ci_run_id: str | None = None,
    ci_run_url: str | None = None,
    deploy_status: str | None = None,
    deploy_run_url: str | None = None,
    deploy_commit_sha: str | None = None,
    github_environment_name: str | None = None,
    last_workflow_sync_at: str | None = None,
) -> None:
    kwargs: dict[str, Any] = {}
    if ci_status is not None:
        kwargs["ci_status"] = ci_status
    if ci_run_id is not None:
        kwargs["ci_run_id"] = ci_run_id
    if ci_run_url is not None:
        kwargs["ci_run_url"] = ci_run_url
    if deploy_status is not None:
        kwargs["deploy_status"] = deploy_status
    if deploy_run_url is not None:
        kwargs["deploy_run_url"] = deploy_run_url
    if deploy_commit_sha is not None:
        kwargs["deploy_commit_sha"] = deploy_commit_sha
    if github_environment_name is not None:
        kwargs["github_environment_name"] = github_environment_name
    if last_workflow_sync_at is not None:
        kwargs["last_workflow_sync_at"] = last_workflow_sync_at
    if kwargs:
        await update_project(project_id, **kwargs)


async def update_project_deploy_status(
    project_id: str,
    deploy_status: str,
    *,
    last_deploy_at: str | None = None,
    last_deploy_run_id: str | None = None,
    deploy_run_url: str | None = None,
    deploy_commit_sha: str | None = None,
) -> None:
    kwargs: dict[str, Any] = {"deploy_status": deploy_status}
    if last_deploy_at is not None:
        kwargs["last_deploy_at"] = last_deploy_at
    if last_deploy_run_id is not None:
        kwargs["last_deploy_run_id"] = last_deploy_run_id
    if deploy_run_url is not None:
        kwargs["deploy_run_url"] = deploy_run_url
    if deploy_commit_sha is not None:
        kwargs["deploy_commit_sha"] = deploy_commit_sha
    await update_project(project_id, **kwargs)


async def record_approval(
    project_id: str,
    approval_type: str,
    approved: bool,
    *,
    feedback: str | None = None,
) -> None:
    project = await get_project(project_id)
    if not project:
        return

    history = list(project.get("approval_history", []))
    history.append(
        {
            "type": approval_type,
            "approved": approved,
            "feedback": feedback,
            "timestamp": _now(),
        }
    )

    update_kwargs: dict[str, Any] = {"approval_history": history}
    if approval_type == "plan" and approved:
        update_kwargs["plan_approved"] = True
        update_kwargs["plan_approved_at"] = _now()
    if approval_type == "preview" and approved:
        update_kwargs["preview_approved"] = True
        update_kwargs["preview_approved_at"] = _now()

    await update_project(project_id, **update_kwargs)


async def sync_project_from_state(project_id: str, state: dict[str, Any]) -> None:
    """Persist the relevant graph snapshot back into the project record."""
    update_kwargs: dict[str, Any] = {}

    if "plan_md" in state:
        update_kwargs["plan_md"] = state.get("plan_md") or ""
    if "stack_config" in state:
        update_kwargs["stack_config"] = state.get("stack_config") or {}
    if "llm_config" in state:
        update_kwargs["llm_config"] = state.get("llm_config") or default_llm_config()
    if "execution_contract" in state:
        update_kwargs["execution_contract"] = state.get("execution_contract") or {}
    if "contract_version" in state:
        update_kwargs["contract_version"] = state.get("contract_version") or 0
    if "decision_log" in state:
        update_kwargs["decision_log"] = state.get("decision_log") or []
    if "plan_delta_history" in state:
        update_kwargs["plan_delta_history"] = state.get("plan_delta_history") or []
    if "container_id" in state:
        update_kwargs["container_id"] = state.get("container_id") or None
    if "container_port" in state:
        update_kwargs["port"] = state.get("container_port") or None
    if "project_stage" in state:
        update_kwargs["project_stage"] = state.get("project_stage") or "intake"
    if "status" in state:
        update_kwargs["status"] = state.get("status") or "pending"
    if "active_task_id" in state:
        update_kwargs["active_task_id"] = state.get("active_task_id") or None
    if "task_attempt_count" in state:
        update_kwargs["task_attempt_count"] = state.get("task_attempt_count") or 0
    if "task_validation_result" in state:
        update_kwargs["task_validation_result"] = state.get("task_validation_result") or {}
    if "last_escalation" in state:
        update_kwargs["last_escalation"] = state.get("last_escalation") or {}
    if "builder_report" in state:
        update_kwargs["builder_report"] = state.get("builder_report") or {}
    if "repo_name" in state:
        update_kwargs["repo_name"] = state.get("repo_name") or None
    if "repo_url" in state:
        update_kwargs["repo_url"] = state.get("repo_url") or None
    if "repo_clone_url" in state:
        update_kwargs["repo_clone_url"] = state.get("repo_clone_url") or None
    if "default_branch" in state:
        update_kwargs["default_branch"] = state.get("default_branch") or None
    if "develop_branch" in state:
        update_kwargs["develop_branch"] = state.get("develop_branch") or None
    if "feature_branch" in state:
        update_kwargs["feature_branch"] = state.get("feature_branch") or None
    if "repo_ready" in state:
        update_kwargs["repo_ready"] = state.get("repo_ready", False)
    if "preview_url" in state:
        update_kwargs["preview_url"] = state.get("preview_url") or None
    if "preview_status" in state:
        update_kwargs["preview_status"] = state.get("preview_status") or "pending"
    if "preview_metadata" in state:
        update_kwargs["preview_metadata"] = state.get("preview_metadata") or {}
    if "ci_status" in state:
        update_kwargs["ci_status"] = state.get("ci_status") or "not_configured"
    if "ci_run_id" in state:
        update_kwargs["ci_run_id"] = state.get("ci_run_id") or None
    if "ci_run_url" in state:
        update_kwargs["ci_run_url"] = state.get("ci_run_url") or None
    if "deploy_run_url" in state:
        update_kwargs["deploy_run_url"] = state.get("deploy_run_url") or None
    if "deploy_commit_sha" in state:
        update_kwargs["deploy_commit_sha"] = state.get("deploy_commit_sha") or None
    if "github_environment_name" in state:
        update_kwargs["github_environment_name"] = state.get("github_environment_name") or None
    if "last_workflow_sync_at" in state:
        update_kwargs["last_workflow_sync_at"] = state.get("last_workflow_sync_at") or None
    if "plan_approved" in state:
        update_kwargs["plan_approved"] = state.get("plan_approved", False)
    if "preview_approved" in state:
        update_kwargs["preview_approved"] = state.get("preview_approved", False)
    if "plan_approved_at" in state:
        update_kwargs["plan_approved_at"] = state.get("plan_approved_at")
    if "preview_approved_at" in state:
        update_kwargs["preview_approved_at"] = state.get("preview_approved_at")

    if update_kwargs:
        if "preview_status" in update_kwargs:
            update_kwargs["preview_checked_at"] = _now()
        await update_project(project_id, **update_kwargs)


async def save_chat_message(message_id: str, project_id: str, role: str, content: str) -> None:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            """
            INSERT INTO chat_messages (id, project_id, role, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (message_id, project_id, role, content, _now()),
        )
        await db.commit()


async def get_chat_messages(project_id: str) -> list[dict[str, Any]]:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM chat_messages WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def save_task_log(
    log_id: str,
    session_id: str,
    task_index: int,
    task_text: str,
    status: str,
    cline_output: str,
    duration_s: float,
) -> None:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            """
            INSERT INTO task_logs
                (id, session_id, task_index, task_text, status, cline_output, duration_s, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (log_id, session_id, task_index, task_text, status, cline_output, duration_s, _now()),
        )
        await db.commit()


async def get_task_logs(project_id: str) -> list[dict[str, Any]]:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT task_logs.*
            FROM task_logs
            JOIN sessions ON sessions.id = task_logs.session_id
            WHERE sessions.project_id = ?
            ORDER BY task_logs.created_at
            """,
            (project_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def save_deploy_run(
    run_id: str,
    project_id: str,
    status: str,
    branch: str,
    output: str,
    summary: dict[str, Any] | None = None,
    *,
    deployed_at: str | None = None,
) -> None:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            """
            INSERT INTO deploy_runs (id, project_id, status, branch, output, summary_json, created_at, deployed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                project_id,
                status,
                branch,
                output,
                _json_dumps(summary or {}),
                _now(),
                deployed_at,
            ),
        )
        await db.commit()


async def upsert_deploy_run(
    run_id: str,
    project_id: str,
    status: str,
    branch: str,
    output: str,
    summary: dict[str, Any] | None = None,
    *,
    deployed_at: str | None = None,
) -> None:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT created_at FROM deploy_runs WHERE id = ?", (run_id,))
        existing = await cursor.fetchone()
        created_at = existing["created_at"] if existing else _now()
        await db.execute(
            """
            INSERT OR REPLACE INTO deploy_runs
                (id, project_id, status, branch, output, summary_json, created_at, deployed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                project_id,
                status,
                branch,
                output,
                _json_dumps(summary or {}),
                created_at,
                deployed_at,
            ),
        )
        await db.commit()


async def get_deploy_runs(project_id: str) -> list[dict[str, Any]]:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM deploy_runs WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        )
        rows = await cursor.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["summary"] = _json_loads(item.pop("summary_json", "{}"), {})
            result.append(item)
        return result


async def save_trace_event(
    event_id: str,
    project_id: str,
    actor: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
    stage: str | None = None,
) -> None:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            """
            INSERT INTO trace_events
                (id, project_id, session_id, actor, event_type, stage, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                project_id,
                session_id,
                actor,
                event_type,
                stage,
                _json_dumps(payload),
                _now(),
            ),
        )
        await db.commit()


async def create_github_issue(
    issue_id: str,
    project_id: str,
    issue_number: int,
    title: str,
    *,
    epic: str | None = None,
    story: str | None = None,
    copilot_workspace_url: str | None = None,
) -> dict[str, Any]:
    await _ensure_db_ready()
    now = _now()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            """
            INSERT INTO github_issues
                (id, project_id, issue_number, title, epic, story, status,
                 copilot_workspace_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
            """,
            (issue_id, project_id, issue_number, title, epic, story,
             copilot_workspace_url, now, now),
        )
        await db.commit()
    return await _get_github_issue(project_id, issue_number)  # type: ignore[return-value]


async def _get_github_issue(project_id: str, issue_number: int) -> dict[str, Any] | None:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM github_issues WHERE project_id = ? AND issue_number = ?",
            (project_id, issue_number),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        item = dict(row)
        item["pr_review"] = _json_loads(item.pop("pr_review_json", None), {})
        return item


async def list_github_issues(project_id: str) -> list[dict[str, Any]]:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM github_issues WHERE project_id = ? ORDER BY issue_number",
            (project_id,),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["pr_review"] = _json_loads(item.pop("pr_review_json", None), {})
            result.append(item)
        return result


async def record_webhook_delivery(delivery_id: str) -> bool:
    """Atomically record a webhook delivery.

    Returns True if this is the first time we've seen `delivery_id` (caller should
    proceed). Returns False if it was already recorded (caller should skip — GitHub
    is retrying after a timeout and we've already processed the work).
    """
    if not delivery_id:
        return True  # nothing to dedupe by; caller must process
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        try:
            await db.execute(
                "INSERT INTO webhook_deliveries (delivery_id, received_at) VALUES (?, ?)",
                (delivery_id, _now()),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def find_github_issue_by_repo(
    owner: str, repo_name: str, issue_number: int
) -> dict[str, Any] | None:
    """Find a github_issue record by repo owner/name + issue number (across all projects)."""
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT gi.* FROM github_issues gi
            JOIN projects p ON gi.project_id = p.id
            WHERE p.github_repo_owner = ? AND p.github_repo_name = ? AND gi.issue_number = ?
            """,
            (owner, repo_name, issue_number),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        item = dict(row)
        item["pr_review"] = _json_loads(item.pop("pr_review_json", None), {})
        return item


async def update_github_issue_status(
    project_id: str, issue_number: int, status: str
) -> None:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        if status == "implementing":
            # Stamp when we entered this state so the startup sweeper can recognise stale rows
            await db.execute(
                "UPDATE github_issues SET status = ?, updated_at = ?, implementing_started_at = ? "
                "WHERE project_id = ? AND issue_number = ?",
                (status, _now(), _now(), project_id, issue_number),
            )
        else:
            await db.execute(
                "UPDATE github_issues SET status = ?, updated_at = ?, implementing_started_at = NULL "
                "WHERE project_id = ? AND issue_number = ?",
                (status, _now(), project_id, issue_number),
            )
        await db.commit()


async def update_github_issue_build_status(
    project_id: str, issue_number: int, build_status: str
) -> None:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "UPDATE github_issues SET build_status = ?, updated_at = ? WHERE project_id = ? AND issue_number = ?",
            (build_status, _now(), project_id, issue_number),
        )
        await db.commit()


async def update_github_issue_pr(
    project_id: str,
    issue_number: int,
    pr_number: int,
    pr_url: str,
    status: str = "pr_open",
    pr_review: dict[str, Any] | None = None,
) -> None:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            """
            UPDATE github_issues
            SET pr_number = ?, pr_url = ?, status = ?,
                pr_review_json = ?, updated_at = ?
            WHERE project_id = ? AND issue_number = ?
            """,
            (
                pr_number,
                pr_url,
                status,
                _json_dumps(pr_review) if pr_review else None,
                _now(),
                project_id,
                issue_number,
            ),
        )
        await db.commit()


async def get_trace_events(project_id: str) -> list[dict[str, Any]]:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM trace_events
            WHERE project_id = ?
            ORDER BY created_at
            """,
            (project_id,),
        )
        rows = await cursor.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _json_loads(item.pop("payload_json", "{}"), {})
            result.append(item)
        return result


async def save_activity_log(
    project_id: str,
    seq: int,
    task_text: str,
    output: str = "",
    status: str = "INFO",
) -> None:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            """
            INSERT INTO activity_logs (id, project_id, seq, task_text, output, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(__import__("uuid").uuid4()), project_id, seq, task_text, output, status, _now()),
        )
        await db.commit()


async def get_activity_logs(project_id: str) -> list[dict[str, Any]]:
    await _ensure_db_ready()
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM activity_logs WHERE project_id = ? ORDER BY seq ASC",
            (project_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
