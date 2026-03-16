"""Helpers for project-level LLM configuration."""

from __future__ import annotations

from typing import Literal

from orchestrator.config import settings

LlmProvider = Literal["openai", "anthropic", "google"]
LlmRole = Literal["architect", "builder", "reviewer"]

SUPPORTED_PROVIDERS: tuple[LlmProvider, ...] = ("openai", "anthropic", "google")


def infer_provider_from_model(model: str, fallback: LlmProvider = "openai") -> LlmProvider:
    normalized = (model or "").strip().lower()
    if normalized.startswith("anthropic/") or "claude" in normalized:
        return "anthropic"
    if normalized.startswith("gemini/") or normalized.startswith("google/") or "gemini" in normalized:
        return "google"
    if normalized.startswith("openai/") or normalized.startswith("gpt-") or normalized.startswith("o"):
        return "openai"
    return fallback


def normalize_provider(provider: str | None, fallback: LlmProvider = "openai") -> LlmProvider:
    value = (provider or "").strip().lower()
    if value in SUPPORTED_PROVIDERS:
        return value  # type: ignore[return-value]
    return fallback


def normalize_model_identifier(provider: str, model: str) -> str:
    selected_provider = normalize_provider(provider)
    cleaned = (model or "").strip()
    if not cleaned:
        return default_model_for_role("architect") if selected_provider == "openai" else ""
    if "/" in cleaned:
        return cleaned
    if selected_provider == "anthropic":
        return f"anthropic/{cleaned}"
    if selected_provider == "google":
        return f"gemini/{cleaned}"
    return cleaned


def provider_api_key(provider: str) -> str:
    selected_provider = normalize_provider(provider)
    if selected_provider == "anthropic":
        return settings.anthropic_api_key
    if selected_provider == "google":
        return settings.google_api_key
    return settings.openai_api_key


def builder_auth_provider(provider: str) -> str:
    selected_provider = normalize_provider(provider)
    if selected_provider == "openai":
        return "openai-native"
    return "gemini" if selected_provider == "google" else selected_provider


def default_model_for_role(role: LlmRole) -> str:
    if role == "builder":
        return settings.builder_model
    if role == "reviewer":
        return settings.reviewer_model
    return settings.architect_model


def default_llm_config() -> dict[str, dict[str, str]]:
    return {
        role: {
            "provider": infer_provider_from_model(default_model_for_role(role), fallback="openai"),
            "model": default_model_for_role(role),
        }
        for role in ("architect", "builder", "reviewer")
    }


def normalize_llm_config(llm_config: dict | None) -> dict[str, dict[str, str]]:
    defaults = default_llm_config()
    if not isinstance(llm_config, dict):
        return defaults

    result: dict[str, dict[str, str]] = {}
    for role in ("architect", "builder", "reviewer"):
        role_config = llm_config.get(role) if isinstance(llm_config.get(role), dict) else {}
        default_role = defaults[role]
        provider = normalize_provider(
            role_config.get("provider"),  # type: ignore[arg-type]
            fallback=default_role["provider"],  # type: ignore[arg-type]
        )
        model = str(role_config.get("model") or default_role["model"]).strip()
        result[role] = {
            "provider": provider,
            "model": normalize_model_identifier(provider, model),
        }
    return result
