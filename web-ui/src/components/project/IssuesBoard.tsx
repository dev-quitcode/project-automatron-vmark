"use client";

import { useState, useMemo } from "react";
import type { GithubIssue } from "@/lib/types";
import { IssueCard } from "./IssueCard";
import {
  ExternalLink, RefreshCw, ChevronDown, ChevronRight,
  ScanSearch, GitPullRequest, GitMerge, Circle, CheckCircle2,
} from "lucide-react";

interface IssuesBoardProps {
  issues: GithubIssue[];
  repoUrl: string | null;
  onSync: () => void;
  onAudit: () => void;
  onReview: (issueNumber: number, prNumber: number) => void;
  onAssignCopilot: (issueNumber: number) => void;
  reviewingIssues: Set<number>;
  assigningIssues: Set<number>;
  isSyncing: boolean;
  isAuditing: boolean;
}

// Active statuses sort before idle ones
const STATUS_ORDER: Record<string, number> = {
  pr_reviewed: 0,
  pr_open: 1,
  open: 2,
  closed: 3,
  merged: 4,
};

function sortIssues(issues: GithubIssue[]): GithubIssue[] {
  return [...issues].sort((a, b) => {
    const so = (STATUS_ORDER[a.status] ?? 2) - (STATUS_ORDER[b.status] ?? 2);
    if (so !== 0) return so;
    return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
  });
}

export function IssuesBoard({
  issues, repoUrl, onSync, onAudit, onReview, onAssignCopilot,
  reviewingIssues, assigningIssues, isSyncing, isAuditing,
}: IssuesBoardProps) {

  // Group by epic, sort issues within each
  const epicMap = useMemo(() => {
    const map = new Map<string, GithubIssue[]>();
    for (const issue of issues) {
      const epic = issue.epic ?? "General";
      if (!map.has(epic)) map.set(epic, []);
      map.get(epic)!.push(issue);
    }
    // Sort each epic's issues: active first, then by updated_at
    map.forEach((v, k) => map.set(k, sortIssues(v)));
    return map;
  }, [issues]);

  // Auto-collapse epics that are fully done
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
        <p className="text-sm text-muted-foreground">
          {totalDone}/{issues.length} done
        </p>
        <div className="flex items-center gap-2">
          {repoIssuesUrl && (
            <a
              href={repoIssuesUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
            >
              <ExternalLink className="h-3 w-3" />
              GitHub Issues
            </a>
          )}
          <button
            onClick={onAudit}
            disabled={isAuditing}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50"
          >
            <ScanSearch className={`h-3 w-3 ${isAuditing ? "animate-pulse" : ""}`} />
            {isAuditing ? "Auditing..." : "Audit Code"}
          </button>
          <button
            onClick={onSync}
            disabled={isSyncing}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50"
          >
            <RefreshCw className={`h-3 w-3 ${isSyncing ? "animate-spin" : ""}`} />
            {isSyncing ? "Syncing..." : "Sync"}
          </button>
        </div>
      </div>

      {/* Progress bar */}
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary transition-all"
          style={{ width: `${issues.length > 0 ? (totalDone / issues.length) * 100 : 0}%` }}
        />
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
            <GitPullRequest className="h-3 w-3" /> {counts.pr_open} PR open
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
        const epicDone = epicIssues.filter(
          (i) => i.status === "merged" || i.status === "closed"
        ).length;
        const isCollapsed = collapsedEpics.has(epic);
        const epicComplete = epicDone === epicIssues.length;

        return (
          <div key={epic} className="overflow-hidden rounded-xl border border-border">
            <button
              onClick={() => toggleEpic(epic)}
              className="flex w-full items-center justify-between gap-3 bg-muted/40 px-4 py-3 text-left hover:bg-muted/60"
            >
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
                <div
                  className={`h-full rounded-full transition-all ${epicComplete ? "bg-green-500" : "bg-primary"}`}
                  style={{ width: `${epicIssues.length > 0 ? (epicDone / epicIssues.length) * 100 : 0}%` }}
                />
              </div>
            </button>

            {!isCollapsed && (
              <div className="divide-y divide-border/40">
                {epicIssues.map((issue) => (
                  <IssueCard
                    key={issue.id}
                    issue={issue}
                    onReview={onReview}
                    onAssignCopilot={onAssignCopilot}
                    isReviewing={reviewingIssues.has(issue.issue_number)}
                    isAssigning={assigningIssues.has(issue.issue_number)}
                  />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
