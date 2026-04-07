"""LLM provider — model-agnostic wrapper around litellm."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

import litellm
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from orchestrator.config import settings
from orchestrator.observability import trace_event

logger = logging.getLogger(__name__)

# Suppress litellm debug logging
litellm.set_verbose = False


_MODEL_MAX_OUTPUT: dict[str, int] = {
    "gpt-4-turbo": 4096,
    "gpt-4-turbo-2024-04-09": 4096,
    "gpt-4-0125-preview": 4096,
    "gpt-4-1106-preview": 4096,
    "gpt-4-0613": 4096,
    "gpt-4": 4096,
}


def _cap_max_tokens(model: str, max_tokens: int) -> int:
    """Clamp max_tokens to the model's output limit."""
    bare = (model or "").strip().lower().split("/")[-1]
    cap = _MODEL_MAX_OUTPUT.get(bare)
    if cap:
        return min(max_tokens, cap)
    return max_tokens


def _completion_kwargs(model: str, *, temperature: float, max_tokens: int, stream: bool = False) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": _cap_max_tokens(model, max_tokens),
    }
    normalized = (model or "").strip().lower()
    # GPT-5 Codex variants reject non-default temperature values; use the provider-supported value.
    if normalized.startswith("gpt-5"):
        kwargs["temperature"] = 1
    else:
        kwargs["temperature"] = temperature
    if stream:
        kwargs["stream"] = True
    return kwargs


def _messages_to_dicts(messages: list[AnyMessage]) -> list[dict[str, str]]:
    """Convert LangChain messages to litellm dict format."""
    result = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            result.append({"role": "system", "content": msg.content})
        elif isinstance(msg, HumanMessage):
            result.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            result.append({"role": "assistant", "content": msg.content})
        else:
            result.append({"role": "user", "content": str(msg.content)})
    return result


async def call_llm(
    messages: list[AnyMessage],
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 16384,
    trace_context: dict[str, Any] | None = None,
) -> str:
    """Call LLM via litellm (non-streaming, returns full response).

    Args:
        messages: List of LangChain messages
        model: Model identifier (e.g., "claude-opus-4-20250918", "gpt-5.3-codex")
        temperature: Sampling temperature
        max_tokens: Maximum tokens in response

    Returns:
        Full response text from the LLM
    """
    model = model or settings.architect_model
    msg_dicts = _messages_to_dicts(messages)

    logger.info("LLM call: model=%s, messages=%d", model, len(msg_dicts))
    if trace_context and trace_context.get("project_id"):
        await trace_event(
            trace_context["project_id"],
            trace_context.get("actor", "llm"),
            "llm.call.started",
            {
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": msg_dicts,
                "message_count": len(msg_dicts),
                "prompt_name": trace_context.get("prompt_name"),
            },
            session_id=trace_context.get("session_id"),
            stage=trace_context.get("stage"),
        )

    try:
        request_kwargs = _completion_kwargs(
            model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        response = await litellm.acompletion(
            messages=msg_dicts,
            **request_kwargs,
        )
        content = response.choices[0].message.content or ""

        # Log usage
        usage = response.usage
        if usage:
            logger.info(
                "LLM response: model=%s, input_tokens=%d, output_tokens=%d",
                model,
                usage.prompt_tokens,
                usage.completion_tokens,
            )
        if trace_context and trace_context.get("project_id"):
            await trace_event(
                trace_context["project_id"],
                trace_context.get("actor", "llm"),
                "llm.call.completed",
                {
                    "model": model,
                    "response": content,
                    "usage": {
                        "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                        "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                    },
                },
                session_id=trace_context.get("session_id"),
                stage=trace_context.get("stage"),
            )

        return content

    except Exception as e:
        logger.error("LLM call failed (model=%s): %s", model, e)
        if trace_context and trace_context.get("project_id"):
            await trace_event(
                trace_context["project_id"],
                trace_context.get("actor", "llm"),
                "llm.call.failed",
                {"model": model, "error": str(e)},
                session_id=trace_context.get("session_id"),
                stage=trace_context.get("stage"),
            )
        raise


async def call_llm_streaming(
    messages: list[AnyMessage],
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 8192,
    trace_context: dict[str, Any] | None = None,
) -> AsyncGenerator[str, None]:
    """Call LLM via litellm with streaming (yields tokens).

    Args:
        messages: List of LangChain messages
        model: Model identifier
        temperature: Sampling temperature
        max_tokens: Maximum tokens in response

    Yields:
        Token strings as they arrive from the LLM
    """
    model = model or settings.architect_model
    msg_dicts = _messages_to_dicts(messages)

    logger.info("LLM streaming call: model=%s, messages=%d", model, len(msg_dicts))
    if trace_context and trace_context.get("project_id"):
        await trace_event(
            trace_context["project_id"],
            trace_context.get("actor", "llm"),
            "llm.stream.started",
            {
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": msg_dicts,
                "message_count": len(msg_dicts),
                "prompt_name": trace_context.get("prompt_name"),
            },
            session_id=trace_context.get("session_id"),
            stage=trace_context.get("stage"),
        )

    try:
        request_kwargs = _completion_kwargs(
            model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        response = await litellm.acompletion(
            messages=msg_dicts,
            **request_kwargs,
        )

        collected_chunks: list[str] = []
        async for chunk in response:
            content = chunk.choices[0].delta.content
            if content:
                collected_chunks.append(content)
                yield content

        if trace_context and trace_context.get("project_id"):
            await trace_event(
                trace_context["project_id"],
                trace_context.get("actor", "llm"),
                "llm.stream.completed",
                {
                    "model": model,
                    "response": "".join(collected_chunks),
                    "chunk_count": len(collected_chunks),
                },
                session_id=trace_context.get("session_id"),
                stage=trace_context.get("stage"),
            )

    except Exception as e:
        logger.error("LLM streaming call failed (model=%s): %s", model, e)
        if trace_context and trace_context.get("project_id"):
            await trace_event(
                trace_context["project_id"],
                trace_context.get("actor", "llm"),
                "llm.stream.failed",
                {"model": model, "error": str(e)},
                session_id=trace_context.get("session_id"),
                stage=trace_context.get("stage"),
            )
        raise
