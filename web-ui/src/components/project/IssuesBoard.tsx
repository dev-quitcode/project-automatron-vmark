"use client";

import { useState } from "react";
import type { GithubIssue } from "@/lib/types";
import { IssueCard } from "./IssueCard";
import { RefreshCw, ChevronDown, ChevronRight } from "lucide-react";

interface IssuesBoardProps {
  issues: GithubIssue[];
  onSync: () => void;
  onReview: (issueNumber: number, prNumber: number) => void;
  reviewingIssues: Set<number>;
  isSyncing: boolean;
}

export function IssuesBoard({ issues, onSync, onReview, reviewingIssues, isSyncing }: IssuesBoardProps) {
  const [collapsedEpics, setCollapsedEpics] = useState<Set<string>>(new Set());

  const toggleEpic = (epic: string) => {
    setCollapsedEpics((prev) => {
      const next = new Set(prev);
      next.has(epic) ? next.delete(epic) : next.add(epic);
      return next;
    });
  };

  // Group issues by epic
  const epicMap = new Map<string, GithubIssue[]>();
  for (const issue of issues) {
    const epic = issue.epic ?? "General";
    if (!epicMap.has(epic)) epicMap.set(epic, []);
    epicMap.get(epic)!.push(issue);
  }

  const totalDone = issues.filter(
    (i) => i.status === "merged" || i.status === "closed"
  ).length;

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
      {/* Header row */}
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {totalDone}/{issues.length} tasks done
        </p>
        <button
          onClick={onSync}
          disabled={isSyncing}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50"
        >
          <RefreshCw className={`h-3 w-3 ${isSyncing ? "animate-spin" : ""}`} />
          {isSyncing ? "Syncing..." : "Sync from GitHub"}
        </button>
      </div>

      {/* Progress bar */}
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary transition-all"
          style={{ width: `${issues.length > 0 ? (totalDone / issues.length) * 100 : 0}%` }}
        />
      </div>

      {/* Epics */}
      {Array.from(epicMap.entries()).map(([epic, epicIssues]) => {
        const epicDone = epicIssues.filter(
          (i) => i.status === "merged" || i.status === "closed"
        ).length;
        const isCollapsed = collapsedEpics.has(epic);

        return (
          <div key={epic} className="rounded-xl border border-border overflow-hidden">
            {/* Epic header */}
            <button
              onClick={() => toggleEpic(epic)}
              className="flex w-full items-center justify-between gap-3 bg-muted/40 px-4 py-3 text-left hover:bg-muted/60"
            >
              <div className="flex items-center gap-2 min-w-0">
                {isCollapsed ? (
                  <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
                ) : (
                  <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
                )}
                <span className="font-medium text-sm truncate">{epic}</span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {epicDone}/{epicIssues.length}
                </span>
              </div>

              {/* Mini progress bar */}
              <div className="h-1 w-24 shrink-0 overflow-hidden rounded-full bg-border">
                <div
                  className="h-full rounded-full bg-primary transition-all"
                  style={{
                    width: `${epicIssues.length > 0 ? (epicDone / epicIssues.length) * 100 : 0}%`,
                  }}
                />
              </div>
            </button>

            {/* Tasks */}
            {!isCollapsed && (
              <div className="divide-y divide-border/50 px-3 py-2 space-y-1.5">
                {epicIssues.map((issue) => (
                  <IssueCard
                    key={issue.id}
                    issue={issue}
                    onReview={onReview}
                    isReviewing={reviewingIssues.has(issue.issue_number)}
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
