import { create } from "zustand";
import * as api from "@/lib/api";
import type {
  BuilderLog,
  ChatMessage,
  DeployRun,
  DeployTargetRequest,
  GithubIssue,
  PlanProgress,
  Project,
  ProjectLlmConfig,
  ProjectStage,
} from "@/lib/types";

interface ProjectState {
  projects: Project[];
  currentProject: Project | null;
  chatMessages: ChatMessage[];
  builderLogs: BuilderLog[];
  deployRuns: DeployRun[];
  issues: GithubIssue[];
  planMd: string | null;
  isConnected: boolean;
  isLoading: boolean;
  error: string | null;
  humanRequired: boolean;
  humanReason: string | null;
  humanStage: ProjectStage | null;
  progress: PlanProgress | null;

  setProjects: (projects: Project[]) => void;
  setCurrentProject: (project: Project | null) => void;
  patchProject: (projectId: string, patch: Partial<Project>) => void;
  addChatMessage: (message: ChatMessage) => void;
  setChatMessages: (messages: ChatMessage[]) => void;
  addBuilderLog: (log: BuilderLog) => void;
  setBuilderLogs: (logs: BuilderLog[]) => void;
  clearBuilderLogs: () => void;
  setDeployRuns: (deployRuns: DeployRun[]) => void;
  setPlanMd: (planMd: string | null) => void;
  setConnected: (connected: boolean) => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string | null) => void;
  setHumanRequired: (
    required: boolean,
    reason?: string,
    stage?: ProjectStage | null
  ) => void;
  setProgress: (progress: PlanProgress | null) => void;
  setIssues: (issues: GithubIssue[]) => void;
  updateIssue: (issue: GithubIssue) => void;

  fetchProjects: () => Promise<void>;
  fetchProject: (id: string) => Promise<void>;
  createProject: (
    name: string,
    repoUrl: string,
    llmConfig: ProjectLlmConfig,
    figmaUrls?: string[]
  ) => Promise<Project>;
  startProject: (id: string) => Promise<void>;
  stopProject: (id: string) => Promise<void>;
  approvePlan: (id: string, feedback?: string) => Promise<void>;
  approvePreview: (id: string, feedback?: string) => Promise<void>;
  deployProject: (id: string) => Promise<void>;
  syncCicd: (id: string) => Promise<void>;
  fetchChatHistory: (projectId: string) => Promise<void>;
  fetchLogs: (projectId: string) => Promise<void>;
  fetchDeployRuns: (projectId: string) => Promise<void>;
  fetchPlan: (projectId: string) => Promise<void>;
  updatePlan: (projectId: string, planMd: string) => Promise<void>;
  updateProjectLlmConfig: (projectId: string, llmConfig: ProjectLlmConfig) => Promise<void>;
  restartPreview: (id: string) => Promise<void>;
  updateDeployTarget: (
    projectId: string,
    target: DeployTargetRequest
  ) => Promise<void>;
  fetchIssues: (projectId: string) => Promise<void>;
  syncIssues: (projectId: string) => Promise<void>;
  auditProject: (projectId: string) => Promise<void>;
  assignToCopilot: (projectId: string) => Promise<{ assigned: number; failed: number }>;
  assignIssueToCopilot: (projectId: string, issueNumber: number) => Promise<void>;
  implementWithAider: (projectId: string, issueNumber: number) => Promise<void>;
  triggerPRReview: (projectId: string, issueNumber: number, prNumber: number) => Promise<void>;
  createIssueFromPrompt: (projectId: string, prompt: string) => Promise<void>;
  buildFailure: { errorSummary: string; defaultBranch: string } | null;
  setBuildFailure: (f: { errorSummary: string; defaultBranch: string } | null) => void;
  createBuildFailureIssue: (projectId: string) => Promise<void>;
}

function getHumanReason(stage?: ProjectStage | null): string | null {
  if (stage === "awaiting_plan_approval") {
    return "Review and approve the technical plan.";
  }
  if (stage === "awaiting_preview_approval") {
    return "Preview is ready. Review it before promoting to develop.";
  }
  return null;
}

function applyProjectPatch(
  project: Project | null,
  projectId: string,
  patch: Partial<Project>
): Project | null {
  if (!project || project.id !== projectId) {
    return project;
  }
  return { ...project, ...patch };
}

export const useProjectStore = create<ProjectState>((set, get) => ({
  projects: [],
  currentProject: null,
  chatMessages: [],
  builderLogs: [],
  deployRuns: [],
  issues: [],
  planMd: null,
  isConnected: false,
  isLoading: false,
  error: null,
  humanRequired: false,
  humanReason: null,
  humanStage: null,
  progress: null,
  buildFailure: null,

  setProjects: (projects) => set({ projects }),
  setCurrentProject: (project) =>
    set({
      currentProject: project,
      planMd: project?.plan_md ?? null,
      humanRequired:
        project?.project_stage === "awaiting_plan_approval" ||
        project?.project_stage === "awaiting_preview_approval",
      humanReason: getHumanReason(project?.project_stage ?? null),
      humanStage:
        project?.project_stage === "awaiting_plan_approval" ||
        project?.project_stage === "awaiting_preview_approval"
          ? project.project_stage
          : null,
    }),
  patchProject: (projectId, patch) =>
    set((state) => ({
      projects: state.projects.map((project) =>
        project.id === projectId ? { ...project, ...patch } : project
      ),
      currentProject: applyProjectPatch(state.currentProject, projectId, patch),
    })),
  addChatMessage: (message) =>
    set((state) => ({ chatMessages: [...state.chatMessages, message] })),
  setChatMessages: (messages) => set({ chatMessages: messages }),
  addBuilderLog: (log) =>
    set((state) => ({ builderLogs: [...state.builderLogs, log] })),
  setBuilderLogs: (builderLogs) => set({ builderLogs }),
  clearBuilderLogs: () => set({ builderLogs: [] }),
  setDeployRuns: (deployRuns) => set({ deployRuns }),
  setPlanMd: (planMd) => set({ planMd }),
  setConnected: (connected) => set({ isConnected: connected }),
  setLoading: (loading) => set({ isLoading: loading }),
  setError: (error) => set({ error }),
  setHumanRequired: (required, reason, stage) =>
    set({
      humanRequired: required,
      humanReason: reason || getHumanReason(stage),
      humanStage: required ? stage || null : null,
    }),
  setProgress: (progress) => set({ progress }),
  setIssues: (issues) => set({ issues }),
  updateIssue: (issue) =>
    set((state) => ({
      issues: state.issues.some((i) => i.id === issue.id)
        ? state.issues.map((i) => (i.id === issue.id ? issue : i))
        : [...state.issues, issue],
    })),

  fetchProjects: async () => {
    set({ isLoading: true, error: null });
    try {
      const projects = await api.getProjects();
      set({ projects, isLoading: false });
    } catch (error: any) {
      set({ error: error.message, isLoading: false });
    }
  },

  fetchProject: async (id) => {
    set({ isLoading: true, error: null });
    try {
      const project = await api.getProject(id);
      set((state) => ({
        projects: state.projects.some((item) => item.id === project.id)
          ? state.projects.map((item) => (item.id === project.id ? project : item))
          : [project, ...state.projects],
        currentProject: project,
        planMd: project.plan_md,
        humanRequired:
          project.project_stage === "awaiting_plan_approval" ||
          project.project_stage === "awaiting_preview_approval",
        humanReason: getHumanReason(project.project_stage),
        humanStage:
          project.project_stage === "awaiting_plan_approval" ||
          project.project_stage === "awaiting_preview_approval"
            ? project.project_stage
            : null,
        isLoading: false,
      }));
    } catch (error: any) {
      set({ error: error.message, isLoading: false });
    }
  },

  createProject: async (name, repoUrl, llmConfig, figmaUrls) => {
    set({ isLoading: true, error: null });
    try {
      const project = await api.createProject({
        name,
        repo_url: repoUrl,
        source: "manual",
        llm_config: llmConfig,
        figma_urls: figmaUrls?.filter(Boolean) ?? [],
      });
      set((state) => ({
        projects: [...state.projects, project],
        isLoading: false,
      }));
      return project;
    } catch (error: any) {
      set({ error: error.message, isLoading: false });
      throw error;
    }
  },

  startProject: async (id) => {
    set({ error: null });
    try {
      await api.startProject(id);
      get().patchProject(id, {
        status: "planning",
        project_stage: "planning",
      });
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  stopProject: async (id) => {
    set({ error: null });
    try {
      await api.stopProject(id);
      get().patchProject(id, { status: "paused" });
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  approvePlan: async (id, feedback) => {
    set({
      error: null,
      humanRequired: false,
      humanReason: null,
      humanStage: null,
    });
    try {
      await api.approvePlan(id, feedback);
      get().patchProject(id, {
        plan_approved: true,
        status: "planning",
      });
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  approvePreview: async (id, feedback) => {
    set({
      error: null,
      humanRequired: false,
      humanReason: null,
      humanStage: null,
    });
    try {
      await api.approvePreview(id, feedback);
      get().patchProject(id, {
        preview_approved: true,
        status: "planning",
      });
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  deployProject: async (id) => {
    set({ error: null });
    try {
      const result = await api.deployProject(id);
      const status =
        result.status === "deployed"
          ? "deployed"
          : result.status === "queued" || result.status === "running"
          ? "deploying"
          : result.status === "failed"
          ? "error"
          : "deploying";
      get().patchProject(id, {
        status,
        project_stage: status === "deployed" ? "deployed" : "deploying",
      });
      await get().fetchProject(id);
      await get().fetchDeployRuns(id);
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  syncCicd: async (id) => {
    try {
      const project = await api.syncProjectCicd(id);
      set((state) => ({
        projects: state.projects.some((item) => item.id === project.id)
          ? state.projects.map((item) => (item.id === project.id ? project : item))
          : [project, ...state.projects],
        currentProject: state.currentProject?.id === project.id ? project : state.currentProject,
        planMd: state.currentProject?.id === project.id ? project.plan_md : state.planMd,
      }));
      await get().fetchDeployRuns(id);
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  fetchChatHistory: async (projectId) => {
    try {
      const messages = await api.getChatHistory(projectId);
      set({ chatMessages: messages });
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  fetchLogs: async (projectId) => {
    try {
      const logs = await api.getProjectLogs(projectId);
      set({ builderLogs: logs });
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  fetchDeployRuns: async (projectId) => {
    try {
      const deployRuns = await api.getDeployRuns(projectId);
      set({ deployRuns });
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  fetchPlan: async (projectId) => {
    try {
      const { plan_md } = await api.getProjectPlan(projectId);
      set({ planMd: plan_md });
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  updatePlan: async (projectId, planMd) => {
    try {
      await api.updateProjectPlan(projectId, planMd);
      set({ planMd });
      get().patchProject(projectId, { plan_md: planMd });
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  updateProjectLlmConfig: async (projectId, llmConfig) => {
    set({ error: null });
    try {
      const project = await api.updateProjectLlmConfig(projectId, llmConfig);
      set((state) => ({
        projects: state.projects.map((item) =>
          item.id === project.id ? project : item
        ),
        currentProject:
          state.currentProject?.id === project.id ? project : state.currentProject,
      }));
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  restartPreview: async (id) => {
    set({ error: null });
    try {
      const project = await api.restartProjectPreview(id);
      set((state) => ({
        projects: state.projects.map((item) =>
          item.id === project.id ? project : item
        ),
        currentProject:
          state.currentProject?.id === project.id ? project : state.currentProject,
      }));
    } catch (error: any) {
      set({ error: error.message });
      throw error;
    }
  },

  updateDeployTarget: async (projectId, target) => {
    try {
      await api.updateDeployTarget(projectId, target);
      get().patchProject(projectId, {
        deploy_status: "configured",
        deploy_target_summary: {
          auth_mode: target.auth_mode,
          host: target.host,
          port: target.port ?? 22,
          user: target.user,
          deploy_path: target.deploy_path,
          auth_reference: target.auth_reference ?? null,
          app_url: target.app_url ?? null,
          health_path: target.health_path ?? null,
        },
      });
    } catch (error: any) {
      set({ error: error.message });
      throw error;
    }
  },

  fetchIssues: async (projectId) => {
    try {
      const issues = await api.getIssues(projectId);
      set({ issues });
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  syncIssues: async (projectId) => {
    try {
      await api.syncIssues(projectId);
      const issues = await api.getIssues(projectId);
      set({ issues });
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  auditProject: async (projectId) => {
    try {
      await api.auditProject(projectId);
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  assignToCopilot: async (projectId) => {
    const result = await api.assignToCopilot(projectId);
    const issues = await api.getIssues(projectId);
    set({ issues });
    return result;
  },

  assignIssueToCopilot: async (projectId, issueNumber) => {
    try {
      await api.assignIssueToCopilot(projectId, issueNumber);
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  implementWithAider: async (projectId, issueNumber) => {
    try {
      await api.implementWithAider(projectId, issueNumber);
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  triggerPRReview: async (projectId, issueNumber, prNumber) => {
    try {
      await api.reviewPR(projectId, issueNumber, prNumber);
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  createIssueFromPrompt: async (projectId, prompt) => {
    try {
      await api.createIssueFromPrompt(projectId, prompt);
    } catch (error: any) {
      set({ error: error.message });
    }
  },

  setBuildFailure: (f) => set({ buildFailure: f }),

  createBuildFailureIssue: async (projectId) => {
    const { buildFailure } = get();
    if (!buildFailure) return;
    try {
      await api.createBuildFailureIssue(projectId, buildFailure.errorSummary, buildFailure.defaultBranch);
      set({ buildFailure: null });
    } catch (error: any) {
      set({ error: error.message });
    }
  },
}));
