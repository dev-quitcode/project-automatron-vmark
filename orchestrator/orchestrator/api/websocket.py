"""WebSocket (Socket.IO) event handlers and graph emit helpers."""

from __future__ import annotations

import logging
import uuid
from urllib.parse import parse_qs

from orchestrator.api.socket_server import sio
from orchestrator.models.project import save_chat_message
from orchestrator.observability import trace_event

logger = logging.getLogger(__name__)


def _project_room(project_id: str) -> str:
    return f"project:{project_id}"


@sio.on("connect")
async def on_connect(sid: str, environ: dict) -> None:
    query = environ.get("QUERY_STRING", "")
    params = parse_qs(query)
    project_id = (params.get("projectId") or params.get("project_id") or [None])[0]
    if project_id:
        await sio.enter_room(sid, _project_room(project_id))
    logger.info("Client connected: %s", sid)


@sio.on("disconnect")
async def on_disconnect(sid: str) -> None:
    logger.info("Client disconnected: %s", sid)


@sio.on("join")
async def on_join(sid: str, data: dict) -> None:
    project_id = data.get("project_id") or data.get("projectId")
    if project_id:
        await sio.enter_room(sid, _project_room(project_id))


@sio.on("leave")
async def on_leave(sid: str, data: dict) -> None:
    project_id = data.get("project_id") or data.get("projectId")
    if project_id:
        await sio.leave_room(sid, _project_room(project_id))


@sio.on("chat:message")
async def on_chat_message(sid: str, data: dict) -> None:
    project_id = data.get("project_id") or data.get("projectId")
    text = data.get("message") or data.get("text") or ""
    if not project_id or not text:
        return

    await save_chat_message(str(uuid.uuid4()), project_id, "user", text)
    await trace_event(
        project_id,
        "operator",
        "chat.message.received",
        {"text": text},
    )
    await sio.emit(
        "architect:message",
        {
            "project_id": project_id,
            "content": f"[Automatron] Message queued for the next planning/build review:\n\n{text}",
            "is_streaming": False,
        },
        room=_project_room(project_id),
    )


async def emit_architect_message(project_id: str, content: str, streaming: bool = False) -> None:
    await sio.emit(
        "architect:message",
        {
            "project_id": project_id,
            "content": content,
            "is_streaming": streaming,
        },
        room=_project_room(project_id),
    )


async def emit_architect_chunk(project_id: str, chunk: str) -> None:
    """Emit a single streaming token from the architect for real-time UI updates."""
    await sio.emit(
        "architect:message",
        {
            "project_id": project_id,
            "content": chunk,
            "is_streaming": True,
        },
        room=_project_room(project_id),
    )


async def emit_builder_log(
    project_id: str,
    *,
    task_index: int,
    task_text: str,
    output: str,
    status: str,
) -> None:
    await sio.emit(
        "builder:log",
        {
            "project_id": project_id,
            "task_index": task_index,
            "task_text": task_text,
            "output": output,
            "status": status,
        },
        room=_project_room(project_id),
    )


async def emit_status_update(
    project_id: str,
    *,
    status: str,
    stage: str,
    progress: dict,
    preview_url: str | None = None,
) -> None:
    payload = {
        "project_id": project_id,
        "status": status,
        "stage": stage,
        "progress": progress,
    }
    if preview_url:
        payload["preview_url"] = preview_url
    await sio.emit("status:update", payload, room=_project_room(project_id))


async def emit_human_required(project_id: str, reason: str, *, stage: str | None = None) -> None:
    payload = {"project_id": project_id, "reason": reason}
    if stage:
        payload["stage"] = stage
    await sio.emit("human:required", payload, room=_project_room(project_id))


async def emit_error(project_id: str, message: str, *, stage: str = "error") -> None:
    await sio.emit(
        "run:error",
        {"project_id": project_id, "message": message, "stage": stage},
        room=_project_room(project_id),
    )


async def emit_plan_updated(project_id: str, plan_md: str) -> None:
    await sio.emit(
        "plan:updated",
        {"project_id": project_id, "plan_md": plan_md},
        room=_project_room(project_id),
    )


async def emit_issues_updated(project_id: str, issues: list) -> None:
    """Fires after apply_plan and sync_issues with the current issue list."""
    await sio.emit(
        "issues:updated",
        {"project_id": project_id, "issues": issues},
        room=_project_room(project_id),
    )


async def emit_pr_review_ready(
    project_id: str,
    issue_number: int,
    pr_number: int,
    passed: bool,
    summary: str,
) -> None:
    """Fires after an AI PR review completes."""
    await sio.emit(
        "pr:review_ready",
        {
            "project_id": project_id,
            "issue_number": issue_number,
            "pr_number": pr_number,
            "passed": passed,
            "summary": summary,
        },
        room=_project_room(project_id),
    )


async def emit_build_failed(project_id: str, error_summary: str, default_branch: str) -> None:
    """Fires when a post-merge build check fails — lets the frontend ask the user before creating an issue."""
    await sio.emit(
        "build:failed",
        {
            "project_id": project_id,
            "error_summary": error_summary,
            "default_branch": default_branch,
        },
        room=_project_room(project_id),
    )


async def emit_build_passed(project_id: str, default_branch: str) -> None:
    """Fires when a post-merge build check passes."""
    await sio.emit(
        "build:passed",
        {"project_id": project_id, "default_branch": default_branch},
        room=_project_room(project_id),
    )


async def emit_aider_needs_help(
    project_id: str,
    issue_number: int,
    error_summary: str,
) -> None:
    """Fires when Aider's pre-push build check fails but main is clean — user decides next step."""
    await sio.emit(
        "aider:needs_help",
        {
            "project_id": project_id,
            "issue_number": issue_number,
            "error_summary": error_summary,
        },
        room=_project_room(project_id),
    )
