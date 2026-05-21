import type {
  BuilderLog,
  ChatMessage,
  DeployRun,
  DeployTargetRequest,
  GithubIssue,
  LlmProvider,
  ProviderModelCatalog,
  PreflightResult,
  Project,
  ProjectCreateRequest,
  Session,
  UpdateProjectLlmConfigRequest,
} from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    throw new Error(`API ${res.status}: ${await res.text()}`);
  }
  return res.json();
}

function mapChatMessage(message: any): ChatMessage {
  return {
    id: message.id,
    project_id: message.project_id,
    role: message.role,
    content: message.content,
    timestamp: message.created_at,
  };
}

function mapBuilderLog(log: any): BuilderLog {
  return {
    project_id: log.project_id || "",
    task_index: log.task_index,
    task_text: log.task_text || "",
    status: log.status,
    output: log.output || log.cline_output || "",
    error_detail: log.error_detail || null,
    timestamp: log.created_at || new Date().toISOString(),
  };
}

export async function getProjects(): Promise<Project[]> {
  return request("/api/projects");
}

export async function getProject(id: string): Promise<Project> {
  return request(`/api/projects/${id}`);
}

export async function getProviderModels(
  provider: LlmProvider,
  forceRefresh = false
): Promise<ProviderModelCatalog> {
  const suffix = forceRefresh ? "?force_refresh=true" : "";
  return request(`/api/llm/providers/${provider}/models${suffix}`);
}

export async function syncProjectCicd(id: string): Promise<Project> {
  return request(`/api/projects/${id}/sync-cicd`, {
    method: "POST",
  });
}

export async function runProjectPreflight(
  projectId: string,
  phase: "start" | "deploy"
): Promise<PreflightResult> {
  return request(`/api/projects/${projectId}/preflight`, {
    method: "POST",
    body: JSON.stringify({ phase }),
  });
}

export async function createProject(data: ProjectCreateRequest): Promise<Project> {
  return request("/api/projects", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function deleteProject(id: string): Promise<void> {
  await request(`/api/projects/${id}`, { method: "DELETE" });
}

export async function startProject(projectId: string): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/start`, { method: "POST" });
}

export async function stopProject(projectId: string): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/stop`, { method: "POST" });
}

export async function approvePlan(
  projectId: string,
  feedback?: string
): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/approve-plan`, {
    method: "POST",
    body: JSON.stringify({ feedback }),
  });
}

export async function approvePreview(
  projectId: string,
  feedback?: string
): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/approve-preview`, {
    method: "POST",
    body: JSON.stringify({ feedback }),
  });
}

export async function deployProject(
  projectId: string
): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/deploy`, {
    method: "POST",
  });
}

export async function restartProjectPreview(projectId: string): Promise<Project> {
  return request(`/api/projects/${projectId}/preview/restart`, {
    method: "POST",
  });
}

export async function getProjectPlan(
  projectId: string
): Promise<{ plan_md: string | null }> {
  return request(`/api/projects/${projectId}/plan`);
}

export async function updateProjectPlan(
  projectId: string,
  planMd: string
): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/plan`, {
    method: "PUT",
    body: JSON.stringify({ plan_md: planMd }),
  });
}

export async function updateProjectLlmConfig(
  projectId: string,
  llmConfig: UpdateProjectLlmConfigRequest
): Promise<Project> {
  return request(`/api/projects/${projectId}/llm-config`, {
    method: "PUT",
    body: JSON.stringify(llmConfig),
  });
}

export async function getChatHistory(
  projectId: string
): Promise<ChatMessage[]> {
  const messages = await request<any[]>(`/api/projects/${projectId}/chat-history`);
  return messages.map(mapChatMessage);
}

export async function getProjectLogs(
  projectId: string
): Promise<BuilderLog[]> {
  const logs = await request<any[]>(`/api/projects/${projectId}/logs`);
  return logs.map(mapBuilderLog);
}

export async function getProjectSessions(
  projectId: string
): Promise<Session[]> {
  return request(`/api/projects/${projectId}/sessions`);
}

export async function getPreviewUrl(
  projectId: string
): Promise<{ preview_url: string | null }> {
  return request(`/api/projects/${projectId}/preview-url`);
}

export async function rollbackProject(
  projectId: string,
  checkpointId: string
): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/rollback`, {
    method: "POST",
    body: JSON.stringify({ checkpoint_id: checkpointId }),
  });
}

export async function updateDeployTarget(
  projectId: string,
  target: DeployTargetRequest
): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/deploy-target`, {
    method: "PUT",
    body: JSON.stringify(target),
  });
}

export async function getDeployRuns(projectId: string): Promise<DeployRun[]> {
  return request(`/api/projects/${projectId}/deploy-runs`);
}

export async function getIssues(projectId: string): Promise<GithubIssue[]> {
  return request(`/api/projects/${projectId}/issues`);
}

export async function syncIssues(projectId: string): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/sync-issues`, { method: "POST" });
}

export async function auditProject(projectId: string): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/audit`, { method: "POST" });
}

export async function runProjectBuildCheck(projectId: string): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/build-check`, { method: "POST" });
}

export async function uploadFigmaFile(
  projectId: string,
  file: File
): Promise<{ status: string; chars: number }> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${API_URL}/api/projects/${projectId}/figma-file`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw new Error(`Figma upload failed: ${await res.text()}`);
  return res.json();
}

export async function assignToCopilot(
  projectId: string
): Promise<{ assigned: number; failed: number }> {
  return request(`/api/projects/${projectId}/assign-copilot`, { method: "POST" });
}

export async function assignIssueToCopilot(
  projectId: string,
  issueNumber: number
): Promise<{ assigned: number; issue_number: number }> {
  return request(`/api/projects/${projectId}/issues/${issueNumber}/assign-copilot`, { method: "POST" });
}

export async function implementWithAider(
  projectId: string,
  issueNumber: number
): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/issues/${issueNumber}/implement`, { method: "POST" });
}

export async function reviewPR(
  projectId: string,
  issueNumber: number,
  prNumber: number
): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/review-pr`, {
    method: "POST",
    body: JSON.stringify({ issue_number: issueNumber, pr_number: prNumber }),
  });
}

export async function createIssueFromPrompt(
  projectId: string,
  prompt: string
): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/issues/create-from-prompt`, {
    method: "POST",
    body: JSON.stringify({ prompt }),
  });
}

export async function createBuildFailureIssue(
  projectId: string,
  errorSummary: string,
  defaultBranch: string
): Promise<{ status: string }> {
  return request(`/api/projects/${projectId}/build-failure-issue`, {
    method: "POST",
    body: JSON.stringify({ error_summary: errorSummary, default_branch: defaultBranch }),
  });
}
