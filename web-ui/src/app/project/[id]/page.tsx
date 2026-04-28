"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import * as api from "@/lib/api";
import { AppLayout } from "@/components/layout";
import { ChatPanel } from "@/components/project/ChatPanel";
import { IssuesBoard } from "@/components/project/IssuesBoard";
import { PlanEditor } from "@/components/project/PlanEditor";
import {
  cloneProjectLlmConfig,
  defaultProjectLlmConfig,
  llmProviders,
} from "@/lib/llmOptions";
import { AlertPanel, LogStream, ProgressBar, StatusBadge } from "@/components/ui";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useProjectStore } from "@/stores/projectStore";
import type {
  DeployTargetRequest,
  LlmProvider,
  PreflightResult,
  ProjectLlmConfig,
  ProjectStage,
  ProviderModelCatalog,
} from "@/lib/types";
import {
  ArrowLeft,
  CheckCircle2,
  ExternalLink,
  GitBranch,
  Play,
  Rocket,
  Save,
  Server,
  Square,
  Workflow,
  RefreshCw,
} from "lucide-react";

type ActiveTab = "chat" | "plan" | "issues" | "preview" | "activity" | "deploy";

const stageGroups: { id: ProjectStage; label: string; group: string }[] = [
  { id: "intake", label: "Intake", group: "Intake" },
  { id: "planning", label: "Planning", group: "Plan" },
  { id: "awaiting_plan_approval", label: "Approve Plan", group: "Plan" },
  { id: "repo_preparing", label: "Repository", group: "Build" },
  { id: "scaffolding", label: "Scaffold", group: "Build" },
  { id: "building", label: "Build", group: "Build" },
  {
    id: "awaiting_preview_approval",
    label: "Approve Preview",
    group: "Preview",
  },
  { id: "deployment_planning", label: "Plan deploy", group: "Deploy" },
  { id: "deploy_target_configured", label: "Target set", group: "Deploy" },
  { id: "deployment_artifacts_generated", label: "Artifacts", group: "Deploy" },
  { id: "deployment_preflight_passed", label: "Preflight ok", group: "Deploy" },
  { id: "deployment_preflight_failed", label: "Preflight fail", group: "Deploy" },
  { id: "ready_for_deploy", label: "Ready", group: "Deploy" },
  { id: "deploying", label: "Deploying", group: "Deploy" },
  { id: "deployed", label: "Live", group: "Deploy" },
  { id: "deploy_failed", label: "Failed", group: "Deploy" },
  { id: "rolling_back", label: "Rolling back", group: "Deploy" },
  { id: "rolled_back", label: "Rolled back", group: "Deploy" },
  { id: "frozen", label: "Frozen", group: "Build" },
  { id: "error", label: "Error", group: "Deploy" },
];

export default function ProjectPage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.id as string;

  const [activeTab, setActiveTab] = useState<ActiveTab>("issues");
  const [isSyncingIssues, setIsSyncingIssues] = useState(false);
  const [isAuditing, setIsAuditing] = useState(false);
  const [reviewingIssues, setReviewingIssues] = useState<Set<number>>(new Set());
  const [assigningIssues, setAssigningIssues] = useState<Set<number>>(new Set());
  const [llmConfig, setLlmConfig] = useState<ProjectLlmConfig>(
    cloneProjectLlmConfig(defaultProjectLlmConfig)
  );
  const [providerCatalogs, setProviderCatalogs] = useState<
    Partial<Record<LlmProvider, ProviderModelCatalog>>
  >({});
  const [loadingProviders, setLoadingProviders] = useState<
    Partial<Record<LlmProvider, boolean>>
  >({});
  const [deployTarget, setDeployTarget] = useState<DeployTargetRequest>({
    config: {
      strategy: "kamal",
      host: "",
      ssh_user: "",
      ssh_port: 22,
      domain: "",
      container_port: 3000,
      health_path: "/api/health",
      registry: "ghcr.io",
      registry_username: "",
      image: null,
      clear_env: {},
      secret_env_names: [],
      auto_deploy_on_main: false,
      artifacts_push_mode: "pr",
    },
    secrets: {
      ssh_private_key: "",
      registry_password: "",
      secret_env_values: {},
    },
  });
  const [secretEnvDraft, setSecretEnvDraft] = useState<{ name: string; value: string }>({
    name: "",
    value: "",
  });
  const [isSavingLlmConfig, setIsSavingLlmConfig] = useState(false);
  const [isSavingDeployTarget, setIsSavingDeployTarget] = useState(false);
  const [isGeneratingArtifacts, setIsGeneratingArtifacts] = useState(false);
  const [isRunningDeployPreflight, setIsRunningDeployPreflight] = useState(false);
  const [deployPreflight, setDeployPreflight] = useState<PreflightResult | null>(null);
  const [isDispatchingDeploy, setIsDispatchingDeploy] = useState(false);
  const [isDispatchingSetup, setIsDispatchingSetup] = useState(false);
  const [isDispatchingRollback, setIsDispatchingRollback] = useState(false);

  const {
    currentProject,
    chatMessages,
    builderLogs,
    deployRuns,
    issues,
    planMd,
    humanRequired,
    humanReason,
    humanStage,
    isLoading,
    error,
    progress,
    fetchChatHistory,
    fetchDeployRuns,
    fetchIssues,
    fetchLogs,
    fetchPlan,
    fetchProject,
    startProject,
    stopProject,
    approvePlan,
    approvePreview,
    deployProject,
    syncIssues,
    auditProject,
    restartPreview,
    triggerPRReview,
    assignIssueToCopilot,
    updateDeployTarget,
    updateProjectLlmConfig,
    updatePlan,
    setHumanRequired,
    setError,
  } = useProjectStore();
  const targetSummaryKey = JSON.stringify(currentProject?.deploy_target_summary ?? null);

  const { sendMessage } = useWebSocket(projectId);

  const loadProviderCatalog = async (
    provider: LlmProvider,
    forceRefresh = false
  ): Promise<ProviderModelCatalog | null> => {
    if (!forceRefresh && providerCatalogs[provider]) {
      return providerCatalogs[provider] ?? null;
    }

    setLoadingProviders((current) => ({ ...current, [provider]: true }));
    try {
      const catalog = await api.getProviderModels(provider, forceRefresh);
      setProviderCatalogs((current) => ({ ...current, [provider]: catalog }));
      return catalog;
    } catch {
      return null;
    } finally {
      setLoadingProviders((current) => ({ ...current, [provider]: false }));
    }
  };

  useEffect(() => {
    if (!projectId) {
      return;
    }
    void fetchProject(projectId);
    void fetchChatHistory(projectId);
    void fetchLogs(projectId);
    void fetchDeployRuns(projectId);
    void fetchPlan(projectId);
    void fetchIssues(projectId);
  }, [
    fetchChatHistory,
    fetchDeployRuns,
    fetchIssues,
    fetchLogs,
    fetchPlan,
    fetchProject,
    projectId,
  ]);

  useEffect(() => {
    const providers = new Set<LlmProvider>(
      Object.values(llmConfig).map((config) => config.provider)
    );
    providers.forEach((provider) => {
      void loadProviderCatalog(provider);
    });
  }, [llmConfig.architect.provider, llmConfig.builder.provider, llmConfig.reviewer.provider]);

  useEffect(() => {
    if (!currentProject?.llm_config) {
      return;
    }
    setLlmConfig(cloneProjectLlmConfig(currentProject.llm_config));
  }, [currentProject?.id, currentProject?.llm_config]);

  useEffect(() => {
    const target = currentProject?.deploy_target_summary;
    if (!target || target.strategy !== "kamal") {
      return;
    }
    setDeployTarget((current) => ({
      config: {
        strategy: "kamal",
        host: target.host || "",
        ssh_user: target.ssh_user || "",
        ssh_port: target.ssh_port || 22,
        domain: target.domain || "",
        container_port: target.container_port || 3000,
        health_path: target.health_path || "/api/health",
        registry: "ghcr.io",
        registry_username: target.registry_username || "",
        image: target.image ?? null,
        clear_env: current.config.clear_env,
        secret_env_names: target.secret_names?.filter(
          (name) => name !== "KAMAL_REGISTRY_PASSWORD" && name !== "KAMAL_SSH_PRIVATE_KEY"
        ) ?? [],
        auto_deploy_on_main: Boolean(target.auto_deploy_on_main),
        artifacts_push_mode: target.artifacts_push_mode || "pr",
      },
      secrets: {
        ssh_private_key: "",
        registry_password: "",
        secret_env_values: {},
      },
    }));
  }, [targetSummaryKey]);

  const handleApprove = (feedback?: string) => {
    if (humanStage === "awaiting_preview_approval") {
      return approvePreview(projectId, feedback);
    }
    return approvePlan(projectId, feedback);
  };

  const handleSavePlan = (nextPlanMd: string) => updatePlan(projectId, nextPlanMd);

  const handleSyncIssues = async () => {
    setIsSyncingIssues(true);
    try {
      await syncIssues(projectId);
    } finally {
      setIsSyncingIssues(false);
    }
  };

  const handleAudit = async () => {
    setIsAuditing(true);
    try {
      await auditProject(projectId);
    } finally {
      setIsAuditing(false);
    }
  };

  const handleReviewPR = async (issueNumber: number, prNumber: number) => {
    setReviewingIssues((prev) => new Set(prev).add(issueNumber));
    try {
      await triggerPRReview(projectId, issueNumber, prNumber);
    } finally {
      setReviewingIssues((prev) => {
        const next = new Set(prev);
        next.delete(issueNumber);
        return next;
      });
    }
  };

  const handleAssignCopilot = async (issueNumber: number) => {
    setAssigningIssues((prev) => new Set(prev).add(issueNumber));
    try {
      await assignIssueToCopilot(projectId, issueNumber);
    } finally {
      setAssigningIssues((prev) => {
        const next = new Set(prev);
        next.delete(issueNumber);
        return next;
      });
    }
  };

  const handleSaveLlmConfig = async () => {
    setIsSavingLlmConfig(true);
    try {
      await updateProjectLlmConfig(projectId, llmConfig);
      await fetchProject(projectId);
    } finally {
      setIsSavingLlmConfig(false);
    }
  };

  const updateRoleProvider = async (
    role: keyof ProjectLlmConfig,
    provider: LlmProvider
  ) => {
    setLlmConfig((current) => ({
      ...current,
      [role]: {
        provider,
        model: "",
      },
    }));
    const catalog = await loadProviderCatalog(provider);
    setLlmConfig((current) => ({
      ...current,
      [role]: {
        provider,
        model: catalog?.models[0]?.id ?? "",
      },
    }));
  };

  const modelOptionsFor = (provider: LlmProvider) =>
    providerCatalogs[provider]?.models ?? [];

  const handleSaveDeployTarget = async () => {
    setIsSavingDeployTarget(true);
    try {
      await updateDeployTarget(projectId, deployTarget);
      await fetchProject(projectId);
      // Scrub secrets from UI memory after successful upsert.
      setDeployTarget((current) => ({
        ...current,
        secrets: {
          ssh_private_key: "",
          registry_password: "",
          secret_env_values: {},
        },
      }));
    } finally {
      setIsSavingDeployTarget(false);
    }
  };

  const handleGenerateArtifacts = async () => {
    setIsGeneratingArtifacts(true);
    try {
      await api.generateDeployArtifacts(projectId);
      await fetchProject(projectId);
    } finally {
      setIsGeneratingArtifacts(false);
    }
  };

  const handleRunDeployPreflight = async () => {
    setIsRunningDeployPreflight(true);
    try {
      const result = await api.runDeployPreflight(projectId, "deploy");
      setDeployPreflight(result);
    } finally {
      setIsRunningDeployPreflight(false);
    }
  };

  const handleDispatchSetup = async () => {
    setIsDispatchingSetup(true);
    try {
      await api.setupDeploy(projectId);
      await fetchProject(projectId);
    } finally {
      setIsDispatchingSetup(false);
    }
  };

  const handleDispatchDeploy = async () => {
    setIsDispatchingDeploy(true);
    try {
      await api.deployProject(projectId);
      await fetchProject(projectId);
    } finally {
      setIsDispatchingDeploy(false);
    }
  };

  const handleDispatchRollback = async () => {
    setIsDispatchingRollback(true);
    try {
      await api.rollbackProject(projectId, null);
      await fetchProject(projectId);
    } finally {
      setIsDispatchingRollback(false);
    }
  };

  const updateConfigField = <K extends keyof DeployTargetRequest["config"]>(
    field: K,
    value: DeployTargetRequest["config"][K]
  ) =>
    setDeployTarget((current) => ({
      ...current,
      config: { ...current.config, [field]: value },
    }));

  const updateSecretField = <K extends keyof DeployTargetRequest["secrets"]>(
    field: K,
    value: DeployTargetRequest["secrets"][K]
  ) =>
    setDeployTarget((current) => ({
      ...current,
      secrets: { ...current.secrets, [field]: value },
    }));

  const isRunning = ["planning", "building"].includes(
    currentProject?.status || ""
  );
  const canStart =
    !!currentProject &&
    ["pending", "paused", "error"].includes(currentProject.status);
  const canApprovePlan =
    currentProject?.project_stage === "awaiting_plan_approval";
  const canApprovePreview =
    currentProject?.project_stage === "awaiting_preview_approval";
  const canDeploy = currentProject?.project_stage === "ready_for_deploy";
  const isDeployed = currentProject?.project_stage === "deployed";
  const deployConfigured = Boolean(currentProject?.deploy_target_summary?.host);
  const canRestartPreview =
    Boolean(currentProject?.container_id) &&
    (currentProject?.status === "preview" ||
      ["awaiting_preview_approval", "ready_for_deploy", "deployed"].includes(
        currentProject?.project_stage || ""
      ));
  const currentStageIndex = stageGroups.findIndex(
    (stage) => stage.id === currentProject?.project_stage
  );

  const groupedStages = useMemo(
    () =>
      ["Intake", "Plan", "Build", "Preview", "Deploy"].map((group) => {
        const stages = stageGroups.filter((stage) => stage.group === group);
        const activeStage = stages.find((stage) => stage.id === currentProject?.project_stage);
        const complete =
          stages.length > 0 &&
          stages.every((stage) => {
            const stageIndex = stageGroups.findIndex((item) => item.id === stage.id);
            return currentStageIndex > stageIndex;
          });
        return { group, activeStage, complete };
      }),
    [currentProject?.project_stage, currentStageIndex]
  );

  if (isLoading && !currentProject) {
    return (
      <AppLayout>
        <div className="flex h-full items-center justify-center text-muted-foreground">
          Loading project...
        </div>
      </AppLayout>
    );
  }

  if (!currentProject) {
    return (
      <AppLayout>
        <div className="flex h-full items-center justify-center text-muted-foreground">
          Project not found.
        </div>
      </AppLayout>
    );
  }

  return (
    <AppLayout>
      {error && (
        <div className="mb-4 flex items-center justify-between rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          <span>{error}</span>
          <button onClick={() => setError(null)} className="ml-4 shrink-0 opacity-70 hover:opacity-100">✕</button>
        </div>
      )}
      <div className="mb-4 flex items-center justify-between">
        <button
          onClick={() => router.push("/")}
          className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to projects
        </button>

        <div className="flex items-center gap-2">
          {currentProject.repo_url && (
            <a
              href={currentProject.repo_url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
            >
              <GitBranch className="h-3.5 w-3.5" />
              Repository
            </a>
          )}

          {currentProject.preview_url && (
            <a
              href={currentProject.preview_url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
            >
              <ExternalLink className="h-3.5 w-3.5" />
              Preview
            </a>
          )}

          {canRestartPreview && (
            <button
              onClick={() => void restartPreview(projectId)}
              className="flex items-center gap-2 rounded-lg border border-border px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground"
            >
              <RefreshCw className="h-4 w-4" />
              Restart Preview
            </button>
          )}

          {isDeployed && currentProject.deploy_run_url && (
            <a
              href={currentProject.deploy_run_url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1.5 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-sm text-emerald-700 hover:text-emerald-800"
            >
              <Rocket className="h-3.5 w-3.5" />
              Live Deploy
            </a>
          )}

          {canApprovePlan && (
            <button
              onClick={() => void approvePlan(projectId)}
              className="flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
            >
              <CheckCircle2 className="h-4 w-4" />
              Approve Plan
            </button>
          )}

          {canApprovePreview && (
            <button
              onClick={() => void approvePreview(projectId)}
              className="flex items-center gap-2 rounded-lg bg-cyan-600 px-4 py-2 text-sm font-medium text-white hover:bg-cyan-700"
            >
              <CheckCircle2 className="h-4 w-4" />
              Approve Preview
            </button>
          )}

          {canDeploy && (
            <button
              onClick={() => void deployProject(projectId)}
              disabled={!deployConfigured}
              className="flex items-center gap-2 rounded-lg bg-violet-600 px-4 py-2 text-sm font-medium text-white hover:bg-violet-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Rocket className="h-4 w-4" />
              Deploy
            </button>
          )}

          {canStart && (
            <button
              onClick={() => void startProject(projectId)}
              className="flex items-center gap-2 rounded-lg bg-green-600 px-4 py-2 text-sm font-medium text-white hover:bg-green-700"
            >
              <Play className="h-4 w-4" />
              {currentProject.status === "pending" ? "Start Build" : "Resume"}
            </button>
          )}

          {isRunning && (
            <button
              onClick={() => void stopProject(projectId)}
              className="flex items-center gap-2 rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90"
            >
              <Square className="h-4 w-4" />
              Stop
            </button>
          )}
        </div>
      </div>

      {humanRequired && (
        <div className="mb-4">
          <AlertPanel
            reason={humanReason || "The system needs your review before continuing."}
            onApprove={handleApprove}
            onDismiss={() => setHumanRequired(false)}
          />
        </div>
      )}

      <div className="mb-4 grid gap-4 xl:grid-cols-[1.4fr_1fr]">
          <div className="rounded-xl border border-border bg-card p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
                  Delivery Flow
                </p>
                <h2 className="mt-1 text-lg font-semibold">
                  Intake, plan, preview, deploy
                </h2>
              </div>
              <StatusBadge status={currentProject.status} size="md" />
            </div>

            <div className="mt-4 grid gap-3 md:grid-cols-5">
              {groupedStages.map(({ group, activeStage, complete }) => (
                <div
                  key={group}
                  className={`rounded-xl border px-3 py-3 ${
                    activeStage
                      ? "border-primary/30 bg-primary/5"
                      : complete
                      ? "border-green-500/20 bg-green-500/5"
                      : "border-border bg-background"
                  }`}
                >
                  <p className="text-xs uppercase tracking-wide text-muted-foreground">
                    {group}
                  </p>
                  <p className="mt-2 text-sm font-medium">
                    {activeStage?.label || (complete ? "Complete" : "Queued")}
                  </p>
                </div>
              ))}
            </div>

            <div className="mt-4 grid gap-4 md:grid-cols-2">
              <div className="rounded-xl border border-border bg-background p-4">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <Workflow className="h-4 w-4 text-primary" />
                  Technical Stage
                </div>
                <p className="mt-2 text-sm text-muted-foreground">
                  {currentProject.project_stage.replace(/_/g, " ")}
                </p>
                {isDeployed && (
                  <p className="mt-2 text-xs text-emerald-600">
                    Production rollout completed successfully.
                  </p>
                )}
                {progress && (
                  <div className="mt-4">
                    <ProgressBar
                      total={progress.total}
                      completed={progress.completed}
                    />
                  </div>
                )}
              </div>

              <div className="rounded-xl border border-border bg-background p-4">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <Server className="h-4 w-4 text-primary" />
                  Solomon Intake
                </div>
                <p className="mt-2 line-clamp-5 text-sm text-muted-foreground">
                  {currentProject.intake_text}
                </p>
              </div>
            </div>
          </div>

          <div className="grid gap-4">
            <div className="rounded-xl border border-border bg-card p-4">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
                    LLM Roles
                  </p>
                  <p className="mt-2 text-sm text-muted-foreground">
                    Configure provider and model for architect, builder, and reviewer.
                  </p>
                </div>
                <button
                  onClick={() => void handleSaveLlmConfig()}
                  disabled={isSavingLlmConfig || isRunning}
                  className="flex items-center gap-2 rounded-lg bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                  <Save className="h-4 w-4" />
                  {isSavingLlmConfig ? "Saving..." : "Save"}
                </button>
              </div>

              <div className="mt-4 space-y-4">
                {(["architect", "builder", "reviewer"] as const).map((role) => (
                  <div key={role} className="grid gap-3 md:grid-cols-[96px_1fr_1.2fr]">
                    <div className="self-center text-sm font-medium capitalize">{role}</div>

                    <label className="space-y-1 text-sm">
                      <span className="text-muted-foreground">Provider</span>
                      <select
                        value={llmConfig[role].provider}
                        disabled={isRunning}
                        onChange={(event) => {
                          const provider = event.target.value as LlmProvider;
                          void updateRoleProvider(role, provider);
                        }}
                        className="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                      >
                        {llmProviders.map((provider) => (
                          <option key={provider.value} value={provider.value}>
                            {provider.label}
                          </option>
                        ))}
                      </select>
                    </label>

                    <label className="space-y-1 text-sm">
                      <span className="text-muted-foreground">Model</span>
                      <select
                        value={llmConfig[role].model}
                        disabled={isRunning}
                        onChange={(event) =>
                          setLlmConfig((current) => ({
                            ...current,
                            [role]: {
                              ...current[role],
                            model: event.target.value,
                          },
                        }))
                      }
                      className="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                    >
                        <option value="">
                          {loadingProviders[llmConfig[role].provider]
                            ? "Loading models..."
                            : modelOptionsFor(llmConfig[role].provider).length > 0
                            ? "Select model"
                            : "No models available"}
                        </option>
                        {modelOptionsFor(llmConfig[role].provider).map((model) => (
                          <option key={model.id} value={model.id}>
                            {model.label}
                          </option>
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

            <div className="rounded-xl border border-border bg-card p-4">
              <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
                Repository
              </p>
              <div className="mt-3 space-y-2 text-sm">
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Remote</span>
                  <span className="truncate text-right">
                    {currentProject.repo_url || "Pending"}
                  </span>
                </div>
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Default</span>
                  <span>{currentProject.default_branch || "main"}</span>
                </div>
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Develop</span>
                  <span>{currentProject.develop_branch || "develop"}</span>
                </div>
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Feature</span>
                  <span className="truncate text-right">
                    {currentProject.feature_branch || "Not created"}
                  </span>
                </div>
              </div>
            </div>

            <div className="rounded-xl border border-border bg-card p-4">
              <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
                Preview
              </p>
              <div className="mt-3 space-y-2 text-sm">
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Status</span>
                  <span>{currentProject.preview_status || "pending"}</span>
                </div>
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">URL</span>
                  <span className="truncate text-right">
                    {currentProject.preview_url || "Not ready"}
                  </span>
                </div>
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Probe</span>
                  <span className="truncate text-right">
                    {(currentProject.preview_metadata?.probe_url as string) || "Pending"}
                  </span>
                </div>
              </div>
            </div>

            <div className="rounded-xl border border-border bg-card p-4">
              <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
                Deploy Target
              </p>
              <div className="mt-3 space-y-2 text-sm">
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Strategy</span>
                  <span>
                    {currentProject.deploy_target_summary?.strategy || "not_configured"}
                  </span>
                </div>
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Domain</span>
                  <span className="truncate text-right">
                    {currentProject.deploy_target_summary?.domain ||
                      currentProject.deploy_target_summary?.app_url ||
                      "Not set"}
                  </span>
                </div>
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Host</span>
                  <span>{currentProject.deploy_target_summary?.host || "Not set"}</span>
                </div>
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Status</span>
                  <span>{currentProject.deploy_status || "not_configured"}</span>
                </div>
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Health</span>
                  <span>{currentProject.deploy_target_summary?.health_path || "/api/health"}</span>
                </div>
              </div>
            </div>

            <div className="rounded-xl border border-border bg-card p-4">
              <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
                CI/CD
              </p>
              <div className="mt-3 space-y-2 text-sm">
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">CI</span>
                  <span>{currentProject.ci_status || "not_configured"}</span>
                </div>
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">CD</span>
                  <span>{currentProject.deploy_status || "not_configured"}</span>
                </div>
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Environment</span>
                  <span>{currentProject.github_environment_name || "production"}</span>
                </div>
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Commit</span>
                  <span className="truncate text-right">
                    {currentProject.deploy_commit_sha || "Pending"}
                  </span>
                </div>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                {currentProject.ci_run_url && (
                  <a
                    href={currentProject.ci_run_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
                  >
                    Open CI Run
                  </a>
                )}
                {currentProject.deploy_run_url && (
                  <a
                    href={currentProject.deploy_run_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
                  >
                    Open Deploy Run
                  </a>
                )}
              </div>
            </div>
          </div>
        </div>

      <div className="mb-4 flex gap-1 rounded-lg border border-border bg-muted p-1">
        {(["chat", "plan", "issues", "preview", "activity", "deploy"] as ActiveTab[]).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium capitalize transition-colors ${
              activeTab === tab
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {tab}
            {tab === "issues" && issues.length > 0 && (
              <span className="ml-1.5 rounded-full bg-primary/10 px-1.5 py-0.5 text-xs text-primary">
                {issues.length}
              </span>
            )}
            {tab === "preview" && currentProject.preview_url && (
              <span className="ml-1.5 rounded-full bg-green-500/10 px-1.5 py-0.5 text-xs text-green-500">live</span>
            )}
            {tab === "activity" && builderLogs.length > 0 && (() => {
              const isLive = builderLogs.at(-1)?.status === "RUNNING";
              return (
                <span className={`ml-1.5 rounded-full px-1.5 py-0.5 text-xs ${isLive ? "animate-pulse bg-yellow-500/20 text-yellow-500" : "bg-primary/10 text-primary"}`}>
                  {builderLogs.length}
                </span>
              );
            })()}
            {tab === "deploy" && deployRuns.length > 0 && (
              <span className="ml-1.5 rounded-full bg-primary/10 px-1.5 py-0.5 text-xs text-primary">
                {deployRuns.length}
              </span>
            )}
          </button>
        ))}
      </div>

      <div className="h-[calc(100vh-16rem)]">
        {activeTab === "chat" && (
          <ChatPanel
            messages={chatMessages}
            onSendMessage={sendMessage}
            disabled={!currentProject}
          />
        )}

        {activeTab === "plan" && (
          <PlanEditor
            planMd={planMd}
            onSave={handleSavePlan}
            readOnly={isRunning}
          />
        )}

        {activeTab === "issues" && (
          <div className="overflow-y-auto h-full pr-1">
            <IssuesBoard
              issues={issues}
              repoUrl={currentProject.repo_url}
              onSync={() => void handleSyncIssues()}
              onAudit={() => void handleAudit()}
              onReview={(issueNumber, prNumber) => void handleReviewPR(issueNumber, prNumber)}
              onAssignCopilot={(issueNumber) => void handleAssignCopilot(issueNumber)}
              reviewingIssues={reviewingIssues}
              assigningIssues={assigningIssues}
              isSyncing={isSyncingIssues}
              isAuditing={isAuditing}
            />
          </div>
        )}

        {activeTab === "preview" && (
          <div className="flex h-full flex-col items-center justify-center rounded-xl border border-border bg-card">
            {currentProject.preview_url ? (
              <iframe
                src={currentProject.preview_url}
                className="h-full w-full rounded-xl"
                title="Project Preview"
              />
            ) : (
              <div className="flex flex-col items-center gap-4 text-center px-8">
                <div className="flex h-16 w-16 items-center justify-center rounded-full border border-border bg-muted">
                  <ExternalLink className="h-7 w-7 text-muted-foreground" />
                </div>
                <div>
                  <p className="text-sm font-medium">Preview not available yet</p>
                  <p className="mt-1 text-xs text-muted-foreground max-w-xs">
                    The preview environment will be ready once Copilot finishes building and a preview deployment is triggered.
                  </p>
                </div>
                <div className="flex items-center gap-2 rounded-lg border border-border bg-background px-4 py-2 text-xs text-muted-foreground">
                  <span className="h-2 w-2 rounded-full bg-amber-500/70" />
                  Current stage: {currentProject.project_stage.replace(/_/g, " ")}
                </div>
              </div>
            )}
          </div>
        )}

        {activeTab === "activity" && (
          <LogStream logs={builderLogs} maxHeight="100%" />
        )}

        {activeTab === "deploy" && (
          <div className="grid h-full gap-4 xl:grid-cols-[1fr_1.1fr]">
            <div className="space-y-4">
              <div className="rounded-xl border border-border bg-card p-4">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <h3 className="text-sm font-semibold">Kamal Deploy Target</h3>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Automatron generates Kamal artifacts for this project. Secrets
                      are write-only and are immediately stored in GitHub Environment
                      Secrets — they are never persisted in Automatron.
                    </p>
                  </div>
                  <button
                    onClick={() => void handleSaveDeployTarget()}
                    disabled={isSavingDeployTarget}
                    className="flex items-center gap-2 rounded-lg bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  >
                    <Save className="h-4 w-4" />
                    {isSavingDeployTarget ? "Saving..." : "Save config + secrets"}
                  </button>
                </div>

                <div className="mt-4 grid gap-3 md:grid-cols-2">
                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">Domain</span>
                    <input
                      value={deployTarget.config.domain}
                      onChange={(event) => updateConfigField("domain", event.target.value)}
                      placeholder="app.client.com"
                      className="w-full rounded-lg border border-input bg-background px-3 py-2"
                    />
                  </label>

                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">VPS Host (IP)</span>
                    <input
                      value={deployTarget.config.host}
                      onChange={(event) => updateConfigField("host", event.target.value)}
                      placeholder="123.123.123.123"
                      className="w-full rounded-lg border border-input bg-background px-3 py-2"
                    />
                  </label>

                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">SSH User</span>
                    <input
                      value={deployTarget.config.ssh_user}
                      onChange={(event) => updateConfigField("ssh_user", event.target.value)}
                      placeholder="root"
                      className="w-full rounded-lg border border-input bg-background px-3 py-2"
                    />
                  </label>

                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">SSH Port</span>
                    <input
                      type="number"
                      value={deployTarget.config.ssh_port ?? 22}
                      onChange={(event) =>
                        updateConfigField("ssh_port", Number(event.target.value) || 22)
                      }
                      className="w-full rounded-lg border border-input bg-background px-3 py-2"
                    />
                  </label>

                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">Container Port</span>
                    <input
                      type="number"
                      value={deployTarget.config.container_port ?? 3000}
                      onChange={(event) =>
                        updateConfigField("container_port", Number(event.target.value) || 3000)
                      }
                      className="w-full rounded-lg border border-input bg-background px-3 py-2"
                    />
                  </label>

                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">Health Path</span>
                    <input
                      value={deployTarget.config.health_path ?? ""}
                      onChange={(event) => updateConfigField("health_path", event.target.value)}
                      placeholder="/api/health"
                      className="w-full rounded-lg border border-input bg-background px-3 py-2"
                    />
                  </label>

                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">GHCR Username</span>
                    <input
                      value={deployTarget.config.registry_username}
                      onChange={(event) =>
                        updateConfigField("registry_username", event.target.value)
                      }
                      placeholder="github-username-or-org"
                      className="w-full rounded-lg border border-input bg-background px-3 py-2"
                    />
                  </label>

                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">Image (override)</span>
                    <input
                      value={deployTarget.config.image ?? ""}
                      onChange={(event) =>
                        updateConfigField("image", event.target.value || null)
                      }
                      placeholder="ghcr.io/owner/repo (auto)"
                      className="w-full rounded-lg border border-input bg-background px-3 py-2"
                    />
                  </label>

                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">Artifacts push mode</span>
                    <select
                      value={deployTarget.config.artifacts_push_mode ?? "pr"}
                      onChange={(event) =>
                        updateConfigField(
                          "artifacts_push_mode",
                          event.target.value === "direct" ? "direct" : "pr"
                        )
                      }
                      className="w-full rounded-lg border border-input bg-background px-3 py-2"
                    >
                      <option value="pr">Open PR (recommended)</option>
                      <option value="direct">Push directly to main</option>
                    </select>
                  </label>

                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={deployTarget.config.auto_deploy_on_main ?? false}
                      onChange={(event) =>
                        updateConfigField("auto_deploy_on_main", event.target.checked)
                      }
                    />
                    <span className="text-muted-foreground">
                      Auto-deploy on push to main
                    </span>
                  </label>

                  <label className="space-y-1 text-sm md:col-span-2">
                    <span className="text-muted-foreground">
                      SSH Private Key (write-only)
                    </span>
                    <textarea
                      value={deployTarget.secrets.ssh_private_key}
                      onChange={(event) =>
                        updateSecretField("ssh_private_key", event.target.value)
                      }
                      rows={5}
                      placeholder={
                        currentProject.deployment_secret_names?.includes("KAMAL_SSH_PRIVATE_KEY")
                          ? "••• stored in GitHub Environment Secrets. Paste a new key only to rotate."
                          : "Paste an ed25519 private key — stored in GitHub Environment Secrets, never in Automatron."
                      }
                      className="w-full rounded-lg border border-input bg-background px-3 py-2"
                    />
                  </label>

                  <label className="space-y-1 text-sm md:col-span-2">
                    <span className="text-muted-foreground">
                      GHCR PAT (write-only, write:packages)
                    </span>
                    <input
                      type="password"
                      value={deployTarget.secrets.registry_password}
                      onChange={(event) =>
                        updateSecretField("registry_password", event.target.value)
                      }
                      placeholder={
                        currentProject.deployment_secret_names?.includes("KAMAL_REGISTRY_PASSWORD")
                          ? "••• stored. Paste new value to rotate."
                          : "ghp_..."
                      }
                      className="w-full rounded-lg border border-input bg-background px-3 py-2"
                    />
                  </label>

                  <div className="md:col-span-2 space-y-2 rounded-lg border border-dashed border-border p-3 text-sm">
                    <div className="text-muted-foreground">
                      Application secret env vars (write-only)
                    </div>
                    {(deployTarget.config.secret_env_names ?? []).map((name) => (
                      <div key={name} className="flex items-center gap-2">
                        <span className="font-mono text-xs">{name}</span>
                        <input
                          type="password"
                          value={
                            deployTarget.secrets.secret_env_values?.[name] ?? ""
                          }
                          onChange={(event) =>
                            setDeployTarget((current) => ({
                              ...current,
                              secrets: {
                                ...current.secrets,
                                secret_env_values: {
                                  ...(current.secrets.secret_env_values ?? {}),
                                  [name]: event.target.value,
                                },
                              },
                            }))
                          }
                          placeholder={
                            currentProject.deployment_secret_names?.includes(name)
                              ? "••• stored"
                              : "value"
                          }
                          className="flex-1 rounded-lg border border-input bg-background px-3 py-2"
                        />
                        <button
                          type="button"
                          onClick={() =>
                            setDeployTarget((current) => ({
                              ...current,
                              config: {
                                ...current.config,
                                secret_env_names: (
                                  current.config.secret_env_names ?? []
                                ).filter((n) => n !== name),
                              },
                              secrets: {
                                ...current.secrets,
                                secret_env_values: Object.fromEntries(
                                  Object.entries(
                                    current.secrets.secret_env_values ?? {}
                                  ).filter(([k]) => k !== name)
                                ),
                              },
                            }))
                          }
                          className="rounded-lg border border-border px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
                        >
                          Remove
                        </button>
                      </div>
                    ))}
                    <div className="flex items-center gap-2">
                      <input
                        value={secretEnvDraft.name}
                        onChange={(event) =>
                          setSecretEnvDraft((draft) => ({ ...draft, name: event.target.value }))
                        }
                        placeholder="DATABASE_URL"
                        className="flex-1 rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs"
                      />
                      <input
                        type="password"
                        value={secretEnvDraft.value}
                        onChange={(event) =>
                          setSecretEnvDraft((draft) => ({ ...draft, value: event.target.value }))
                        }
                        placeholder="value"
                        className="flex-1 rounded-lg border border-input bg-background px-3 py-2"
                      />
                      <button
                        type="button"
                        onClick={() => {
                          const name = secretEnvDraft.name.trim().toUpperCase();
                          if (!name) return;
                          setDeployTarget((current) => ({
                            ...current,
                            config: {
                              ...current.config,
                              secret_env_names: Array.from(
                                new Set([...(current.config.secret_env_names ?? []), name])
                              ),
                            },
                            secrets: {
                              ...current.secrets,
                              secret_env_values: {
                                ...(current.secrets.secret_env_values ?? {}),
                                [name]: secretEnvDraft.value,
                              },
                            },
                          }));
                          setSecretEnvDraft({ name: "", value: "" });
                        }}
                        className="rounded-lg border border-border px-3 py-2 text-xs text-muted-foreground hover:text-foreground"
                      >
                        Add
                      </button>
                    </div>
                  </div>
                </div>
              </div>

              <div className="rounded-xl border border-border bg-card p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => void handleGenerateArtifacts()}
                    disabled={isGeneratingArtifacts}
                    className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-accent disabled:opacity-50"
                  >
                    <Workflow className="h-4 w-4" />
                    {isGeneratingArtifacts ? "Generating..." : "Generate artifacts"}
                  </button>
                  <button
                    onClick={() => void handleRunDeployPreflight()}
                    disabled={isRunningDeployPreflight}
                    className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-accent disabled:opacity-50"
                  >
                    <CheckCircle2 className="h-4 w-4" />
                    {isRunningDeployPreflight ? "Running..." : "Run preflight"}
                  </button>
                  <button
                    onClick={() => void handleDispatchSetup()}
                    disabled={isDispatchingSetup}
                    className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-accent disabled:opacity-50"
                  >
                    <Server className="h-4 w-4" />
                    {isDispatchingSetup ? "Dispatching..." : "Setup VPS"}
                  </button>
                  <button
                    onClick={() => void handleDispatchDeploy()}
                    disabled={isDispatchingDeploy}
                    className="flex items-center gap-2 rounded-lg bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  >
                    <Rocket className="h-4 w-4" />
                    {isDispatchingDeploy ? "Dispatching..." : "Deploy"}
                  </button>
                  <button
                    onClick={() => void handleDispatchRollback()}
                    disabled={
                      isDispatchingRollback ||
                      currentProject.deploy_status !== "deployed"
                    }
                    className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-accent disabled:opacity-50"
                  >
                    <RefreshCw className="h-4 w-4" />
                    {isDispatchingRollback ? "Dispatching..." : "Rollback"}
                  </button>
                </div>
                {deployPreflight && (
                  <div className="mt-3 space-y-1 text-xs">
                    {deployPreflight.checks.map((check) => (
                      <div
                        key={check.code}
                        className={
                          check.status === "blocking"
                            ? "text-red-500"
                            : check.status === "warning"
                            ? "text-amber-500"
                            : "text-emerald-500"
                        }
                      >
                        [{check.status}] {check.code}: {check.message}
                      </div>
                    ))}
                  </div>
                )}
                {currentProject.deploy_artifacts_fingerprint &&
                  "commit_sha" in (currentProject.deploy_artifacts_fingerprint as object) && (
                    <div className="mt-3 rounded-lg border border-dashed border-border p-3 text-xs">
                      <div className="font-semibold">Artifacts fingerprint</div>
                      <div className="mt-1 space-y-0.5 font-mono text-muted-foreground">
                        <div>
                          branch:{" "}
                          {(currentProject.deploy_artifacts_fingerprint as any).branch}
                        </div>
                        <div>
                          commit:{" "}
                          {(currentProject.deploy_artifacts_fingerprint as any)
                            .commit_sha?.slice(0, 12)}
                        </div>
                        {(currentProject.deploy_artifacts_fingerprint as any).pr_url && (
                          <div>
                            <a
                              href={
                                (currentProject.deploy_artifacts_fingerprint as any).pr_url
                              }
                              target="_blank"
                              rel="noopener noreferrer"
                              className="underline"
                            >
                              Open deploy artifacts PR
                            </a>
                          </div>
                        )}
                      </div>
                    </div>
                  )}
              </div>
            </div>

            <div className="rounded-xl border border-border bg-card p-4">
              <div>
                <h3 className="text-sm font-semibold">Deploy History</h3>
                <p className="mt-1 text-xs text-muted-foreground">
                  Latest rollout attempts reported by GitHub Actions.
                </p>
              </div>

              <div className="mt-4 space-y-3 overflow-auto">
                {deployRuns.length === 0 ? (
                  <div className="rounded-xl border border-dashed border-border px-4 py-6 text-sm text-muted-foreground">
                    No deploy runs yet.
                  </div>
                ) : (
                  deployRuns.map((run) => (
                    <div
                      key={run.id}
                      className="rounded-xl border border-border bg-background p-4"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="text-sm font-medium">{run.branch}</p>
                          <p className="text-xs text-muted-foreground">
                            {run.created_at}
                          </p>
                        </div>
                        <StatusBadge
                          status={
                            run.status === "deployed"
                              ? "deployed"
                              : run.status === "failed"
                              ? "error"
                              : "deploying"
                          }
                        />
                      </div>
                      {run.output && (
                        <pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-card p-3 text-xs text-muted-foreground">
                          {run.output.slice(-3000)}
                        </pre>
                      )}
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </AppLayout>
  );
}
