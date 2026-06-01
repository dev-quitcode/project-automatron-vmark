"use client";

import { useState, useMemo, useRef } from "react";
import type { GithubIssue } from "@/lib/types";
import { IssueCard } from "./IssueCard";
import {
  ExternalLink, RefreshCw, ChevronDown, ChevronRight,
  ScanSearch, GitPullRequest, GitMerge, Circle, CheckCircle2, Monitor, Loader2, Plus, Send, Hammer, XCircle, X, CircleCheck,
} from "lucide-react";

interface IssuesBoardProps {
  issues: GithubIssue[];
  repoUrl: string | null;
  previewUrl: string | null;
  onSync: () => void;
  onAudit: () => void;
  onStartPreview: () => Promise<void> | void;
  onReview: (issueNumber: number, prNumber: number) => void;
  onAssignCopilot: (issueNumber: number) => void;
  onImplementAider: (issueNumber: number) => void;
  onPreviewBranch: (issueNumber: number) => void;
  onCreateIssue: (prompt: string) => void;
  onBuildCheck: () => void;
  reviewingIssues: Set<number>;
  assigningIssues: Set<number>;
  implementingIssues: Set<number>;
  previewingIssues: Set<number>;
  isSyncing: boolean;
  isAuditing: boolean;
  isCreatingIssue: boolean;
  isCheckingBuild: boolean;
  buildFailure: { errorSummary: string; defaultBranch: string } | null;
  onCreateBuildIssue: () => void;
  onDismissBuildFailure: () => void;
  buildPassed: string | null;
  onDismissBuildPassed: () => void;
}

export function IssuesBoard({
  issues, repoUrl, previewUrl, onSync, onAudit, onStartPreview, onReview, onAssignCopilot,
  onImplementAider, onPreviewBranch, onCreateIssue, onBuildCheck,
  reviewingIssues, assigningIssues, implementingIssues, previewingIssues,
  isSyncing, isAuditing, isCreatingIssue, isCheckingBuild,
  buildFailure, onCreateBuildIssue, onDismissBuildFailure,
  buildPassed, onDismissBuildPassed,
}: IssuesBoardProps) {

  const [isStartingPreview, setIsStartingPreview] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [showNewIssueForm, setShowNewIssueForm] = useState(false);
  const [newIssuePrompt, setNewIssuePrompt] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleStartPreview = async () => {
    setIsStartingPreview(true);
    setPreviewError(null);
    try {
      await onStartPreview();
      // Keep spinner until previewUrl arrives via WebSocket (max 3 min)
      setTimeout(() => setIsStartingPreview(false), 180_000);
    } catch (err: any) {
      setIsStartingPreview(false);
      setPreviewError(err?.message ?? "Failed to start preview");
    }
  };

  const handleSubmitNewIssue = () => {
    const prompt = newIssuePrompt.trim();
    if (!prompt || isCreatingIssue) return;
    onCreateIssue(prompt);
    setNewIssuePrompt("");
    setShowNewIssueForm(false);
  };

  // Group by epic — preserve issue_number order (chronological)
  const epicMap = useMemo(() => {
    const map = new Map<string, GithubIssue[]>();
    for (const issue of [...issues].sort((a, b) => a.issue_number - b.issue_number)) {
      const epic = issue.epic ?? "General";
      if (!map.has(epic)) map.set(epic, []);
      map.get(epic)!.push(issue);
    }
    return map;
  }, [issues]);

  // Auto-collapse fully-done epics on first render
  const [collapsedEpics, setCollapsedEpics] = useState<Set<string>>(() => {
    const done = new Set<string>();
    for (const [epic, list] of epicMap) {
      if (list.every((i) => i.status === "merged" || i.status === "closed")) {
        done.add(epic);
      }
    }
    return done;
  });

  const toggleEpic = (epic: string) =>
    setCollapsedEpics((prev) => {
      const next = new Set(prev);
      next.has(epic) ? next.delete(epic) : next.add(epic);
      return next;
    });

  const counts = useMemo(() => ({
    open: issues.filter((i) => i.status === "open").length,
    pr_open: issues.filter((i) => i.status === "pr_open").length,
    pr_reviewed: issues.filter((i) => i.status === "pr_reviewed").length,
    merged: issues.filter((i) => i.status === "merged").length,
    closed: issues.filter((i) => i.status === "closed").length,
  }), [issues]);

  const totalDone = counts.merged + counts.closed;

  const repoIssuesUrl = repoUrl
    ? `${repoUrl.replace(/\.git$/, "")}/issues`
    : issues[0]?.copilot_workspace_url?.replace(/\/issues\/\d+$/, "/issues") ?? null;

  if (issues.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center text-muted-foreground">
        <p className="text-sm">No issues yet.</p>
        <p className="mt-1 text-xs">Approve the plan to generate GitHub Issues.</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">{totalDone}/{issues.length} done</p>
        <div className="flex items-center gap-2">
          {repoIssuesUrl && (
            <a href={repoIssuesUrl} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90">
              <ExternalLink className="h-3 w-3" /> GitHub Issues
            </a>
          )}
          <button onClick={onAudit} disabled={isAuditing}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50">
            <ScanSearch className={`h-3 w-3 ${isAuditing ? "animate-pulse" : ""}`} />
            {isAuditing ? "Auditing..." : "Audit Code"}
          </button>
          <button onClick={onBuildCheck} disabled={isCheckingBuild}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50">
            {isCheckingBuild ? <Loader2 className="h-3 w-3 animate-spin" /> : <Hammer className="h-3 w-3" />}
            {isCheckingBuild ? "Building..." : "Build Check"}
          </button>
          <button onClick={onSync} disabled={isSyncing}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50">
            <RefreshCw className={`h-3 w-3 ${isSyncing ? "animate-spin" : ""}`} />
            {isSyncing ? "Syncing..." : "Sync"}
          </button>
          <button
            onClick={() => { setShowNewIssueForm((v) => !v); setTimeout(() => textareaRef.current?.focus(), 50); }}
            disabled={isCreatingIssue}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50">
            {isCreatingIssue ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}
            {isCreatingIssue ? "Creating..." : "New Issue"}
          </button>
        </div>
      </div>

      {/* New Issue inline form */}
      {showNewIssueForm && (
        <div className="rounded-xl border border-border bg-card p-3 space-y-2">
          <textarea
            ref={textareaRef}
            value={newIssuePrompt}
            onChange={(e) => setNewIssuePrompt(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) handleSubmitNewIssue(); }}
            placeholder="Describe the issue in plain English… e.g. Fix OTP SMS sending — it fails silently when Twilio returns an error"
            rows={3}
            className="w-full resize-none rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
          />
          <div className="flex items-center justify-end gap-2">
            <button onClick={() => { setShowNewIssueForm(false); setNewIssuePrompt(""); }}
              className="text-xs text-muted-foreground hover:text-foreground transition-colors">
              Cancel
            </button>
            <button onClick={handleSubmitNewIssue} disabled={!newIssuePrompt.trim() || isCreatingIssue}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50">
              <Send className="h-3 w-3" /> Create Issue
            </button>
          </div>
        </div>
      )}

      {/* Progress bar */}
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-primary transition-all"
          style={{ width: `${issues.length > 0 ? (totalDone / issues.length) * 100 : 0}%` }} />
      </div>

      {/* Build success banner — auto-dismisses after 6s */}
      {buildPassed && (
        <div className="rounded-xl border border-green-500/20 bg-green-500/5 p-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <CircleCheck className="h-4 w-4 shrink-0 text-green-400" />
            <span className="text-sm font-medium text-green-400">
              Build passed on {buildPassed}
            </span>
          </div>
          <button onClick={onDismissBuildPassed} className="text-muted-foreground hover:text-foreground">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {/* Build failure banner */}
      {buildFailure && (
        <div className="rounded-xl border border-red-500/20 bg-red-500/5 p-4 space-y-3">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-center gap-2">
              <XCircle className="h-4 w-4 shrink-0 text-red-400 mt-0.5" />
              <span className="text-sm font-medium text-red-400">
                Build failed on {buildFailure.defaultBranch}
              </span>
            </div>
            <button onClick={onDismissBuildFailure} className="text-muted-foreground hover:text-foreground">
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          <pre className="rounded-md bg-black/20 px-3 py-2 text-xs text-red-300 whitespace-pre-wrap overflow-auto max-h-32">
            {buildFailure.errorSummary}
          </pre>
          <div className="flex justify-end">
            <button onClick={onCreateBuildIssue}
              className="inline-flex items-center gap-1.5 rounded-md bg-red-500/10 border border-red-500/20 px-3 py-1.5 text-xs font-medium text-red-400 hover:bg-red-500/20 transition-colors">
              <Plus className="h-3 w-3" /> Create GitHub Issue
            </button>
          </div>
        </div>
      )}

      {/* Preview bar — always available */}
      <div className="rounded-xl border border-border bg-card overflow-hidden">
      {previewError && (
        <div className="px-4 py-2 text-xs text-red-400 bg-red-500/5 border-b border-border">
          {previewError}
        </div>
      )}
      <div className="flex items-center justify-between px-4 py-3">
        <div className="flex items-center gap-2">
          <Monitor className="h-4 w-4 text-muted-foreground" />
          <span className="text-sm font-medium">Preview</span>
          {previewUrl && (
            <span className="text-xs text-muted-foreground truncate max-w-[200px]">{previewUrl}</span>
          )}
        </div>
        {previewUrl ? (
          <div className="flex items-center gap-2">
            {isStartingPreview ? (
              <span className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground opacity-60">
                <Loader2 className="h-3 w-3 animate-spin" /> Rebuilding…
              </span>
            ) : (
              <button onClick={handleStartPreview}
                title="Pull latest main and rebuild the preview container"
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted transition-colors">
                <Loader2 className="h-3 w-3" /> Rebuild
              </button>
            )}
            <a href={previewUrl} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 transition-colors">
              <ExternalLink className="h-3 w-3" /> Open Preview
            </a>
          </div>
        ) : isStartingPreview ? (
          <span className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground opacity-60">
            <Loader2 className="h-3 w-3 animate-spin" /> Building…
          </span>
        ) : (
          <button onClick={handleStartPreview}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted transition-colors">
            <Monitor className="h-3 w-3" /> Launch Preview
          </button>
        )}
      </div>
      </div>

      {/* Status summary pills */}
      <div className="flex flex-wrap gap-2 text-xs">
        {counts.open > 0 && (
          <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2.5 py-1 text-muted-foreground">
            <Circle className="h-3 w-3" /> {counts.open} open
          </span>
        )}
        {counts.pr_open > 0 && (
          <span className="inline-flex items-center gap-1 rounded-full bg-blue-500/10 px-2.5 py-1 text-blue-400">
            <GitPullRequest className="h-3 w-3" /> {counts.pr_open} PR ready
          </span>
        )}
        {counts.pr_reviewed > 0 && (
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2.5 py-1 text-amber-400">
            <GitPullRequest className="h-3 w-3" /> {counts.pr_reviewed} reviewed
          </span>
        )}
        {counts.merged > 0 && (
          <span className="inline-flex items-center gap-1 rounded-full bg-purple-500/10 px-2.5 py-1 text-purple-400">
            <GitMerge className="h-3 w-3" /> {counts.merged} merged
          </span>
        )}
        {counts.closed > 0 && (
          <span className="inline-flex items-center gap-1 rounded-full bg-green-500/10 px-2.5 py-1 text-green-400">
            <CheckCircle2 className="h-3 w-3" /> {counts.closed} closed
          </span>
        )}
      </div>

      {/* Epics */}
      {Array.from(epicMap.entries()).map(([epic, epicIssues]) => {
        const epicDone = epicIssues.filter((i) => i.status === "merged" || i.status === "closed").length;
        const isCollapsed = collapsedEpics.has(epic);
        const epicComplete = epicDone === epicIssues.length;

        return (
          <div key={epic} className="overflow-hidden rounded-xl border border-border">
            {/* Epic header */}
            <button onClick={() => toggleEpic(epic)}
              className="flex w-full items-center justify-between gap-3 bg-muted/40 px-4 py-3 text-left hover:bg-muted/60">
              <div className="flex items-center gap-2 min-w-0">
                {isCollapsed
                  ? <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
                  : <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />}
                <span className={`truncate text-sm font-medium ${epicComplete ? "text-muted-foreground" : ""}`}>
                  {epic}
                </span>
                <span className={`shrink-0 text-xs ${epicComplete ? "text-green-400" : "text-muted-foreground"}`}>
                  {epicDone}/{epicIssues.length}
                </span>
              </div>
              <div className="h-1 w-24 shrink-0 overflow-hidden rounded-full bg-border">
                <div className={`h-full rounded-full transition-all ${epicComplete ? "bg-green-500" : "bg-primary"}`}
                  style={{ width: `${epicIssues.length > 0 ? (epicDone / epicIssues.length) * 100 : 0}%` }} />
              </div>
            </button>

            {/* Issues */}
            {!isCollapsed && (
              <>
                <div className="divide-y divide-border/40">
                  {epicIssues.map((issue) => (
                    <IssueCard
                      key={issue.id}
                      issue={issue}
                      onReview={onReview}
                      onAssignCopilot={onAssignCopilot}
                      onImplementAider={onImplementAider}
                      onPreviewBranch={onPreviewBranch}
                      isReviewing={reviewingIssues.has(issue.issue_number)}
                      isAssigning={assigningIssues.has(issue.issue_number)}
                      isImplementing={implementingIssues.has(issue.issue_number)}
                      isPreviewing={previewingIssues.has(issue.issue_number)}
                    />
                  ))}
                </div>

              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
