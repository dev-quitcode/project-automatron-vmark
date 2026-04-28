"use client";

import { useState } from "react";
import type { GithubIssue, IssueStatus } from "@/lib/types";
import {
  ExternalLink, GitPullRequest, CheckCircle2, Circle,
  GitMerge, Loader2, ChevronDown, ChevronUp, XCircle,
} from "lucide-react";

interface IssueCardProps {
  issue: GithubIssue;
  onReview: (issueNumber: number, prNumber: number) => void;
  onAssignCopilot: (issueNumber: number) => void;
  isReviewing: boolean;
  isAssigning: boolean;
}

const STATUS_CONFIG: Record<IssueStatus, { label: string; dot: string; textColor: string; icon: React.ReactNode }> = {
  open: {
    label: "Open",
    dot: "bg-muted-foreground/40",
    textColor: "text-muted-foreground",
    icon: <Circle className="h-3 w-3" />,
  },
  pr_open: {
    label: "PR open",
    dot: "bg-blue-400",
    textColor: "text-blue-400",
    icon: <GitPullRequest className="h-3 w-3" />,
  },
  pr_reviewed: {
    label: "Reviewed",
    dot: "bg-amber-400",
    textColor: "text-amber-400",
    icon: <GitPullRequest className="h-3 w-3" />,
  },
  merged: {
    label: "Merged",
    dot: "bg-purple-400",
    textColor: "text-purple-400",
    icon: <GitMerge className="h-3 w-3" />,
  },
  closed: {
    label: "Closed",
    dot: "bg-green-400",
    textColor: "text-green-400",
    icon: <CheckCircle2 className="h-3 w-3" />,
  },
};

function timeAgo(iso: string): string {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export function IssueCard({ issue, onReview, onAssignCopilot, isReviewing, isAssigning }: IssueCardProps) {
  const [reviewExpanded, setReviewExpanded] = useState(false);
  const cfg = STATUS_CONFIG[issue.status] ?? STATUS_CONFIG.open;
  const isDone = issue.status === "merged" || issue.status === "closed";
  const review = issue.pr_review?.summary ? issue.pr_review : null;

  // Derive direct GitHub issue URL from copilot_workspace_url
  const issueUrl = issue.copilot_workspace_url ?? null;

  return (
    <div className={`px-4 py-3 transition-colors hover:bg-muted/20 ${isDone ? "opacity-55" : ""}`}>
      <div className="flex items-start gap-3">

        {/* Status dot + issue number */}
        <div className="flex shrink-0 flex-col items-center gap-1 pt-0.5">
          <div className={`h-2 w-2 rounded-full ${cfg.dot}`} />
        </div>

        {/* Main content */}
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline gap-2">
                <span className="shrink-0 font-mono text-xs text-muted-foreground">
                  #{issue.issue_number}
                </span>
                <span className={`text-sm leading-snug ${isDone ? "line-through text-muted-foreground" : "font-medium"}`}>
                  {issue.title}
                </span>
              </div>
              {issue.story && (
                <p className="mt-0.5 truncate text-xs text-muted-foreground">{issue.story}</p>
              )}
            </div>

            {/* Status badge */}
            <span className={`inline-flex shrink-0 items-center gap-1 text-xs font-medium ${cfg.textColor}`}>
              {cfg.icon}
              {cfg.label}
            </span>
          </div>

          {/* Action row */}
          <div className="mt-2 flex flex-wrap items-center gap-2">

            {/* View issue on GitHub */}
            {issueUrl && (
              <a
                href={issueUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
              >
                <ExternalLink className="h-3 w-3" />
                Issue
              </a>
            )}

            {/* Open PR */}
            {issue.pr_url && (
              <a
                href={issue.pr_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 rounded-md border border-blue-500/30 bg-blue-500/5 px-2 py-1 text-xs font-medium text-blue-400 transition-colors hover:bg-blue-500/15"
              >
                <GitPullRequest className="h-3 w-3" />
                PR #{issue.pr_number}
                <ExternalLink className="h-2.5 w-2.5" />
              </a>
            )}

            {/* AI Review — when PR is open or reviewed */}
            {(issue.status === "pr_open" || issue.status === "pr_reviewed") && issue.pr_number && (
              <button
                onClick={() => onReview(issue.issue_number, issue.pr_number!)}
                disabled={isReviewing}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1 text-xs font-medium text-foreground transition-colors hover:bg-muted disabled:opacity-50"
              >
                {isReviewing
                  ? <Loader2 className="h-3 w-3 animate-spin" />
                  : <GitPullRequest className="h-3 w-3" />}
                {isReviewing ? "Reviewing…" : "AI Review"}
              </button>
            )}

            {/* Assign Copilot — only when open with no PR */}
            {issue.status === "open" && !issue.pr_url && (
              <button
                onClick={() => onAssignCopilot(issue.issue_number)}
                disabled={isAssigning}
                className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/5 px-2 py-1 text-xs font-medium text-primary transition-colors hover:bg-primary/10 disabled:opacity-50"
              >
                {isAssigning
                  ? <Loader2 className="h-3 w-3 animate-spin" />
                  : <GitPullRequest className="h-3 w-3" />}
                {isAssigning ? "Assigning…" : "Assign Copilot"}
              </button>
            )}

            {/* Updated timestamp */}
            <span className="ml-auto text-xs text-muted-foreground/50">
              {timeAgo(issue.updated_at)}
            </span>
          </div>
        </div>
      </div>

      {/* PR review result */}
      {review && (
        <div className="ml-5 mt-2 rounded-lg border border-border bg-muted/30 px-3 py-2">
          <button
            onClick={() => setReviewExpanded((v) => !v)}
            className="flex w-full items-center justify-between gap-2 text-xs"
          >
            <span className={`flex items-center gap-1.5 font-medium ${review.passed ? "text-green-400" : "text-amber-400"}`}>
              {review.passed
                ? <CheckCircle2 className="h-3.5 w-3.5" />
                : <XCircle className="h-3.5 w-3.5" />}
              {review.passed ? "Review passed" : "Issues found"}
            </span>
            {reviewExpanded
              ? <ChevronUp className="h-3 w-3 text-muted-foreground" />
              : <ChevronDown className="h-3 w-3 text-muted-foreground" />}
          </button>

          {reviewExpanded && (
            <pre className="mt-2 whitespace-pre-wrap text-xs text-muted-foreground leading-relaxed">
              {review.summary}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
