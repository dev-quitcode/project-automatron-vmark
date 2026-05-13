export type ProjectStatus =
  | "pending"
  | "planning"
  | "building"
  | "preview"
  | "ready_for_deploy"
  | "deploying"
  | "deployed"
  | "paused"
  | "frozen"
  | "error"
  | "deleted";

export type ProjectStage =
  | "intake"
  | "planning"
  | "awaiting_plan_approval"
  | "repo_preparing"
  | "scaffolding"
  | "building"
  | "awaiting_preview_approval"
  | "deployment_planning"
  | "deploy_target_configured"
  | "deployment_artifacts_generated"
  | "deployment_preflight_passed"
  | "deployment_preflight_failed"
  | "ready_for_deploy"
  | "deploying"
  | "deployed"
  | "deploy_failed"
  | "rolling_back"
  | "rolled_back"
  | "frozen"
  | "error";

export type DeploymentStrategy = "kamal" | "";
export type ArtifactsPushMode = "pr" | "direct";
export type DeploymentPreflightPhase =
  | "generate_artifacts"
  | "setup"
  | "deploy"
  | "health_verify";

export interface StackConfig {
  [key: string]: unknown;
}

export type LlmProvider = "openai" | "anthropic" | "google";
export type DeployAuthMode = "ssh_key" | "password";

export interface LlmRoleConfig {
  provider: LlmProvider;
  model: string;
}

export interface ProjectLlmConfig {
  architect: LlmRoleConfig;
  builder: LlmRoleConfig;
  reviewer: LlmRoleConfig;
}

export interface DeployTargetSummary {
  strategy: DeploymentStrategy | "legacy";
  // Kamal-shaped fields (present when strategy === "kamal")
  host?: string | null;
  ssh_user?: string | null;
  ssh_port?: number | null;
  domain?: string | null;
  container_port?: number | null;
  health_path?: string | null;
  registry?: string | null;
  registry_username?: string | null;
  image?: string | null;
  clear_env_keys?: string[];
  secret_names?: string[];
  auto_deploy_on_main?: boolean;
  artifacts_push_mode?: ArtifactsPushMode;
  fingerprint?: ArtifactFingerprint | null;
  deploy_audit_issue_number?: number | null;
  deploy_audit_issue_url?: string | null;
  deploy_audit_gate_status?: "missing" | "pending" | "ready" | string;
  // Legacy SSH fields (present when strategy === "legacy")
  auth_mode?: "legacy_unsupported" | DeployAuthMode | null;
  port?: number | null;
  user?: string | null;
  deploy_path?: string | null;
  app_url?: string | null;
}

export interface ArtifactFingerprint {
  commit_sha: string;
  branch: string;
  pr_url: string | null;
  template_version: string;
  strategy_version: string;
  profile_hash: string;
  rendered_files: string[];
}

export interface Project {
  id: string;
  name: string;
  description: string;
  intake_text: string;
  intake_source: string;
  source_ref: string | null;
  status: ProjectStatus;
  project_stage: ProjectStage;
  plan_md: string | null;
  stack_config: StackConfig;
  llm_config: ProjectLlmConfig;
  repo_name: string | null;
  repo_url: string | null;
  figma_urls: string[];
  repo_clone_url: string | null;
  default_branch: string | null;
  develop_branch: string | null;
  feature_branch: string | null;
  repo_ready: boolean;
  container_id: string | null;
  port: number | null;
  preview_url: string | null;
  preview_status: string | null;
  preview_metadata: Record<string, unknown>;
  ci_status: string;
  ci_run_id: string | null;
  ci_run_url: string | null;
  deploy_status: string | null;
  deploy_run_url: string | null;
  deploy_commit_sha: string | null;
  github_environment_name: string | null;
  last_workflow_sync_at: string | null;
  deploy_target_summary: DeployTargetSummary | null;
  deployment_strategy?: string;
  deployment_profile?: Record<string, unknown>;
  deployment_secret_names?: string[];
  deploy_artifacts_fingerprint?: ArtifactFingerprint | Record<string, never>;
  deploy_audit_issue_number?: number | null;
  deploy_audit_issue_url?: string | null;
  deploy_audit_gate_status?: "missing" | "pending" | "ready" | string;
  auto_deploy_on_main?: boolean;
  artifacts_push_mode?: ArtifactsPushMode;
  automatron_deploy_run_id?: string | null;
  last_deploy_run_id?: string | null;
  plan_approved: boolean;
  preview_approved: boolean;
  created_at: string;
  updated_at: string;
}

export type IssueStatus = "open" | "pr_open" | "pr_reviewed" | "merged" | "closed";

export interface PRReview {
  passed: boolean;
  summary: string;
  pr_number: number;
  issue_number: number;
}

export interface GithubIssue {
  id: string;
  project_id: string;
  issue_number: number;
  title: string;
  epic: string | null;
  story: string | null;
  status: IssueStatus;
  pr_number: number | null;
  pr_url: string | null;
  pr_review: PRReview | null;
  copilot_workspace_url: string | null;
  created_at: string;
  updated_at: string;
}

export type MessageRole = "user" | "architect" | "system";

export interface ChatMessage {
  id: string;
  project_id: string;
  role: MessageRole;
  content: string;
  timestamp: string;
}

export type BuilderStatus = "SUCCESS" | "BLOCKER" | "AMBIGUITY" | "SILENT_DECISION" | "ERROR" | "INFO" | "RUNNING";

export interface BuilderLog {
  project_id: string;
  task_index: number;
  task_text: string;
  status: BuilderStatus;
  output: string;
  error_detail: string | null;
  timestamp: string;
}

export interface Session {
  id: string;
  project_id: string;
  started_at: string;
  ended_at: string | null;
  phase: string;
}

export interface DeployRun {
  id: string;
  project_id: string;
  status: string;
  branch: string;
  output: string;
  summary: Record<string, unknown>;
  created_at: string;
  deployed_at: string | null;
}

export interface WsArchitectMessage {
  project_id: string;
  content: string;
  is_streaming: boolean;
}

export interface WsBuilderLog {
  project_id: string;
  task_index: number;
  task_text: string;
  output: string;
  status: BuilderStatus;
}

export interface WsStatusUpdate {
  project_id: string;
  status: ProjectStatus;
  stage: ProjectStage;
  progress?: {
    total?: number;
    completed?: number;
  };
  preview_url?: string | null;
}

export interface WsHumanRequired {
  project_id: string;
  reason: string;
  stage?: ProjectStage;
}

export interface WsPlanUpdated {
  project_id: string;
  plan_md: string;
}

export interface PlanProgress {
  total: number;
  completed: number;
  percentage: number;
}

export interface ProjectCreateRequest {
  name: string;
  repo_url?: string;
  intake_text?: string;
  source?: string;
  source_ref?: string | null;
  llm_config?: ProjectLlmConfig;
  figma_urls?: string[];
}

export interface UpdateProjectLlmConfigRequest extends ProjectLlmConfig {}

export interface LlmCatalogEntry {
  id: string;
  label: string;
}

export interface ProviderModelCatalog {
  provider: LlmProvider;
  configured: boolean;
  models: LlmCatalogEntry[];
  fetched_at: string | null;
  error: string | null;
  cached: boolean;
}

export interface DeployTargetConfig {
  strategy: "kamal";
  host: string;
  ssh_user: string;
  ssh_port?: number;
  domain: string;
  container_port?: number;
  health_path?: string;
  registry?: "ghcr.io";
  registry_username: string;
  image?: string | null;
  clear_env?: Record<string, string>;
  secret_env_names?: string[];
  auto_deploy_on_main?: boolean;
  artifacts_push_mode?: ArtifactsPushMode;
}

export interface DeployTargetSecretsForm {
  /** SSH private key — never persisted. Sent once to upsert into GitHub Env Secrets. */
  ssh_private_key: string;
  /** Container registry password (e.g. GHCR PAT). Never persisted. */
  registry_password: string;
  /** Per-app secret values keyed by env name. Never persisted. */
  secret_env_values?: Record<string, string>;
}

export interface DeployTargetRequest {
  config: DeployTargetConfig;
  secrets: DeployTargetSecretsForm;
}

export interface DeployRequestBody {}

export interface RollbackRequestBody {
  rollback_to?: string | null;
}

export interface DeployPreflightRequestBody {
  phase: DeploymentPreflightPhase;
}

export interface DeployStatus {
  stage: ProjectStage | string;
  status: string;
  url: string | null;
  github_run_id: string | null;
}

export interface PreflightCheck {
  code: string;
  status: "ok" | "warning" | "blocking";
  message: string;
  details: Record<string, unknown>;
}

export interface PreflightResult {
  phase: "start" | "deploy";
  ok: boolean;
  blocking: boolean;
  checks: PreflightCheck[];
}

export interface DeployArtifactsResponse {
  status: "generated";
  project_id: string;
  fingerprint: ArtifactFingerprint;
  deploy_audit_issue: DeployAuditIssue | null;
}

export interface DeployAuditIssue {
  number: number;
  url: string | null;
  state: "open" | "closed" | string;
  gate_status: "missing" | "pending" | "ready" | string;
}
