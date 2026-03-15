"""Structured trace logging for autonomous project runs."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from orchestrator.models.project import save_trace_event

logger = logging.getLogger(__name__)


def _trim(value: Any, *, limit: int = 12000) -> Any:
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return f"{value[:limit]}...[trimmed {len(value) - limit} chars]"
    if isinstance(value, dict):
        return {str(key): _trim(inner, limit=limit) for key, inner in value.items()}
    if isinstance(value, list):
        return [_trim(item, limit=limit) for item in value]
    return value


async def trace_event(
    project_id: str,
    actor: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
    stage: str | None = None,
) -> None:
    if not project_id:
        return
    try:
        await save_trace_event(
            str(uuid.uuid4()),
            project_id,
            actor,
            event_type,
            _trim(payload),
            session_id=session_id,
            stage=stage,
        )
    except Exception as exc:
        logger.warning("Trace event failed (%s/%s): %s", actor, event_type, exc)
