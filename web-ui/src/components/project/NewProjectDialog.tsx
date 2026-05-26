"use client";

import { useCallback, useEffect, useState } from "react";
import * as api from "@/lib/api";
import {
  cloneProjectLlmConfig,
  defaultProjectLlmConfig,
  llmProviders,
} from "@/lib/llmOptions";
import type {
  LlmProvider,
  ProjectLlmConfig,
  ProviderModelCatalog,
} from "@/lib/types";
import { useProjectStore } from "@/stores/projectStore";
import { Plus, Trash2, X } from "lucide-react";

interface NewProjectDialogProps {
  open: boolean;
  onClose: () => void;
}

function parseRepoName(url: string): string {
  // Extract repo name from https://github.com/owner/repo or owner/repo
  const match = url.match(/github\.com\/[^/]+\/([^/?#\s]+)/);
  if (match) return match[1].replace(/\.git$/, "");
  const parts = url.trim().split("/");
  return parts[parts.length - 1]?.replace(/\.git$/, "") ?? "";
}

export function NewProjectDialog({ open, onClose }: NewProjectDialogProps) {
  const [name, setName] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [nameDirty, setNameDirty] = useState(false);
  const [llmConfig, setLlmConfig] = useState<ProjectLlmConfig>(
    cloneProjectLlmConfig(defaultProjectLlmConfig)
  );
  const [providerCatalogs, setProviderCatalogs] = useState<
    Partial<Record<LlmProvider, ProviderModelCatalog>>
  >({});
  const [loadingProviders, setLoadingProviders] = useState<
    Partial<Record<LlmProvider, boolean>>
  >({});
  const [figmaUrls, setFigmaUrls] = useState<string[]>([]);
  const [figmaFile, setFigmaFile] = useState<File | null>(null);
  const [supabaseUrl, setSupabaseUrl] = useState("");
  const [supabaseServiceKey, setSupabaseServiceKey] = useState("");
  const [supabaseAnonKey, setSupabaseAnonKey] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const { createProject } = useProjectStore();

  const loadProviderCatalog = useCallback(
    async (provider: LlmProvider, forceRefresh = false): Promise<ProviderModelCatalog | null> => {
      if (!forceRefresh && providerCatalogs[provider]) {
        return providerCatalogs[provider] ?? null;
      }
      setLoadingProviders((cur) => ({ ...cur, [provider]: true }));
      try {
        const catalog = await api.getProviderModels(provider, forceRefresh);
        setProviderCatalogs((cur) => ({ ...cur, [provider]: catalog }));
        return catalog;
      } catch {
        return null;
      } finally {
        setLoadingProviders((cur) => ({ ...cur, [provider]: false }));
      }
    },
    [providerCatalogs]
  );

  useEffect(() => {
    if (!open) return;
    const providers = new Set<LlmProvider>(
      Object.values(llmConfig).map((c) => c.provider)
    );
    providers.forEach((p) => { void loadProviderCatalog(p); });
  }, [open]);

  // Auto-fill project name from repo URL when the user hasn't typed a name yet
  const handleRepoUrlChange = (value: string) => {
    setRepoUrl(value);
    if (!nameDirty) {
      const suggested = parseRepoName(value);
      if (suggested) setName(suggested);
    }
  };

  if (!open) return null;

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim() || !repoUrl.trim()) return;

    setIsSubmitting(true);
    try {
      const project = await createProject(
        name.trim(),
        repoUrl.trim(),
        llmConfig,
        figmaUrls.filter(Boolean),
        supabaseUrl.trim() || supabaseServiceKey.trim() || supabaseAnonKey.trim()
          ? { url: supabaseUrl, serviceRoleKey: supabaseServiceKey, anonKey: supabaseAnonKey }
          : undefined,
      );
      if (figmaFile && project?.id) {
        await api.uploadFigmaFile(project.id, figmaFile);
      }
      setName("");
      setRepoUrl("");
      setNameDirty(false);
      setFigmaUrls([]);
      setFigmaFile(null);
      setSupabaseUrl("");
      setSupabaseServiceKey("");
      setSupabaseAnonKey("");
      setLlmConfig(cloneProjectLlmConfig(defaultProjectLlmConfig));
      onClose();
    } catch {
      // Error is handled in the store.
    } finally {
      setIsSubmitting(false);
    }
  };

  const updateRoleProvider = async (role: keyof ProjectLlmConfig, provider: LlmProvider) => {
    setLlmConfig((cur) => ({ ...cur, [role]: { provider, model: "" } }));
    const catalog = await loadProviderCatalog(provider);
    setLlmConfig((cur) => ({
      ...cur,
      [role]: { provider, model: catalog?.models[0]?.id ?? "" },
    }));
  };

  const modelOptionsFor = (provider: LlmProvider) =>
    providerCatalogs[provider]?.models ?? [];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />

      <div className="relative w-full max-w-2xl rounded-xl border border-border bg-card p-6 shadow-2xl">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold">Connect Repository</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Paste your GitHub repository URL. Automatron will read the README
              and generate a full technical plan as GitHub Issues.
            </p>
          </div>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
            <X className="h-5 w-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="mt-4 space-y-4">
          <div>
            <label className="mb-1.5 block text-sm font-medium">GitHub Repository URL</label>
            <input
              type="text"
              value={repoUrl}
              onChange={(e) => handleRepoUrlChange(e.target.value)}
              placeholder="https://github.com/your-org/your-repo"
              className="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              autoFocus
            />
            <p className="mt-1 text-xs text-muted-foreground">
              The repo should have a README (and optionally docs/PRD.md) describing what to build.
            </p>
          </div>

          <div>
            <label className="mb-1.5 block text-sm font-medium">Project Name</label>
            <input
              type="text"
              value={name}
              onChange={(e) => { setName(e.target.value); setNameDirty(true); }}
              placeholder="e.g., Invoice Dashboard"
              className="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>

          {/* Figma Design URLs — optional */}
          <div className="rounded-xl border border-border bg-background/60 p-4">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-sm font-semibold">Figma Designs</h3>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Optional — paste Figma URLs or upload a .fig file for design context.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setFigmaUrls((prev) => [...prev, ""])}
                className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
              >
                <Plus className="h-3 w-3" />
                Add URL
              </button>
            </div>

            {figmaUrls.length > 0 && (
              <div className="mt-3 space-y-2">
                {figmaUrls.map((url, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <input
                      type="text"
                      value={url}
                      onChange={(e) =>
                        setFigmaUrls((prev) => prev.map((u, j) => (j === i ? e.target.value : u)))
                      }
                      placeholder="https://www.figma.com/design/..."
                      className="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-xs placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                    />
                    <button
                      type="button"
                      onClick={() => setFigmaUrls((prev) => prev.filter((_, j) => j !== i))}
                      className="shrink-0 text-muted-foreground hover:text-foreground"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}

            <div className="mt-3">
              {figmaFile ? (
                <div className="flex items-center gap-2 rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs">
                  <span className="flex-1 truncate text-foreground">{figmaFile.name}</span>
                  <button
                    type="button"
                    onClick={() => setFigmaFile(null)}
                    className="shrink-0 text-muted-foreground hover:text-foreground"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              ) : (
                <label className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-dashed border-border px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted">
                  <Plus className="h-3 w-3" />
                  Upload .fig file
                  <input
                    type="file"
                    accept=".fig"
                    className="hidden"
                    onChange={(e) => setFigmaFile(e.target.files?.[0] ?? null)}
                  />
                </label>
              )}
            </div>
          </div>

          <div className="rounded-xl border border-border bg-background/60 p-4">
            <div>
              <h3 className="text-sm font-semibold">Supabase (optional)</h3>
              <p className="mt-0.5 text-xs text-muted-foreground">
                Provide your Supabase credentials so Automatron can introspect the live database schema
                and generate code against your real tables — not guesses from migration files.
              </p>
            </div>

            <div className="mt-3 space-y-3">
              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">
                  Project URL
                </label>
                <input
                  type="text"
                  value={supabaseUrl}
                  onChange={(e) => setSupabaseUrl(e.target.value)}
                  placeholder="https://xxxxx.supabase.co"
                  className="w-full rounded-lg border border-input bg-background px-3 py-2 text-xs placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">
                  Service Role Key (used only for schema introspection)
                </label>
                <input
                  type="password"
                  value={supabaseServiceKey}
                  onChange={(e) => setSupabaseServiceKey(e.target.value)}
                  placeholder="eyJhbGciOi..."
                  className="w-full rounded-lg border border-input bg-background px-3 py-2 text-xs placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">
                  Anon Key (written into the project&apos;s .env.local)
                </label>
                <input
                  type="password"
                  value={supabaseAnonKey}
                  onChange={(e) => setSupabaseAnonKey(e.target.value)}
                  placeholder="eyJhbGciOi..."
                  className="w-full rounded-lg border border-input bg-background px-3 py-2 text-xs placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
            </div>
          </div>

          <div className="rounded-xl border border-border bg-background/60 p-4">
            <h3 className="text-sm font-semibold">LLM Configuration</h3>
            <p className="mt-1 text-xs text-muted-foreground">
              Architect: plans issues. Reviewer: reviews PRs.
            </p>

            <div className="mt-4 space-y-4">
              {(["architect", "reviewer"] as const).map((role) => (
                <div key={role} className="grid gap-3 md:grid-cols-[160px_1fr_1.3fr]">
                  <div className="self-center text-sm font-medium capitalize">{role}</div>

                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">Provider</span>
                    <select
                      value={llmConfig[role].provider}
                      onChange={(e) => { void updateRoleProvider(role, e.target.value as LlmProvider); }}
                      className="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                    >
                      {llmProviders.map((p) => (
                        <option key={p.value} value={p.value}>{p.label}</option>
                      ))}
                    </select>
                  </label>

                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">Model</span>
                    <select
                      value={llmConfig[role].model}
                      onChange={(e) =>
                        setLlmConfig((cur) => ({
                          ...cur,
                          [role]: { ...cur[role], model: e.target.value },
                        }))
                      }
                      className="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                      disabled={loadingProviders[llmConfig[role].provider]}
                    >
                      <option value="">
                        {loadingProviders[llmConfig[role].provider]
                          ? "Loading..."
                          : modelOptionsFor(llmConfig[role].provider).length > 0
                          ? "Select model"
                          : "No models available"}
                      </option>
                      {modelOptionsFor(llmConfig[role].provider).map((m) => (
                        <option key={m.id} value={m.id}>{m.label}</option>
                      ))}
                    </select>
                    {providerCatalogs[llmConfig[role].provider]?.error && (
                      <p className="text-xs text-amber-500">
                        {providerCatalogs[llmConfig[role].provider]?.error}
                      </p>
                    )}
                  </label>
                </div>
              ))}
            </div>
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!name.trim() || !repoUrl.trim() || isSubmitting}
              className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
            >
              {isSubmitting ? "Connecting..." : "Connect & Plan"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
