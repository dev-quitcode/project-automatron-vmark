"""Tests for LLM configuration helpers."""

from orchestrator.llm.configuration import builder_auth_provider


def test_builder_auth_provider_maps_openai_to_openai_native() -> None:
    assert builder_auth_provider("openai") == "openai-native"


def test_builder_auth_provider_maps_google_to_gemini() -> None:
    assert builder_auth_provider("google") == "gemini"


def test_builder_auth_provider_preserves_anthropic() -> None:
    assert builder_auth_provider("anthropic") == "anthropic"
