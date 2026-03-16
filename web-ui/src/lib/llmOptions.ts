import type { LlmProvider, ProjectLlmConfig } from "./types";

export const llmProviders: { value: LlmProvider; label: string }[] = [
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "google", label: "Google" },
];

export const defaultProjectLlmConfig: ProjectLlmConfig = {
  architect: {
    provider: "openai",
    model: "gpt-5.3-codex",
  },
  builder: {
    provider: "openai",
    model: "gpt-5.3-codex",
  },
  reviewer: {
    provider: "openai",
    model: "gpt-5.3-codex",
  },
};

export function cloneProjectLlmConfig(config: ProjectLlmConfig): ProjectLlmConfig {
  return {
    architect: { ...config.architect },
    builder: { ...config.builder },
    reviewer: { ...config.reviewer },
  };
}
