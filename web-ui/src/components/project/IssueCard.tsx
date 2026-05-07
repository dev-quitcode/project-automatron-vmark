"use client";

import { useState } from "react";
import type { GithubIssue, IssueStatus } from "@/lib/types";
import {
  ExternalLink, GitPullRequest, CheckCircle2, Circle,
  GitMerge, Loader2, ChevronDown, ChevronUp, XCircle,
  Zap, Eye, ThumbsUp, Bot,
} from "lucide-react";

interface IssueCardProps {
  issue: GithubIssue;
  onReview: (issueNumber: number, prNumber: number) => void;
  onAssignCopilot: (issueNumber: number) => void;
  onImplementAider: (issueNumber: number) => void;
  isReviewing: boolean;
  isAssigning: boolean;
  isImplementing: boolean;
}

type VisualState =
  | "open"
  | "pr_open"
  | "pr_reviewed_pass"
  | "pr_reviewed_fail"
  | "merged"
  | "closed";

function toVisualState(issue: GithubIssue): VisualState {
  if (issue.status === "merged") return "merged";
  if (issue.status === "closed") return "closed";
  if (issue.status === "pr_reviewed") {
    return issue.pr_review?.passed ? "pr_reviewed_pass" : "pr_reviewed_fail";
  }
  if (issue.status === "pr_open") return "pr_open";
  return "open";
}

const STATE_META: Record<VisualState, {
  dot: string;
  label: string;
  labelColor: string;
  rowBg?: string;
}> = {
  open:               { dot: "bg-muted-foreground/30", label: "Open",           labelColor: "text-muted-foreground" },
  pr_open:            { dot: "bg-blue-400",             label: "PR Ready",       labelColor: "text-blue-400",    rowBg: "bg-blue-500/5" },
  pr_reviewed_pass:   { dot: "bg-green-400",            label: "Review Passed",  labelColor: "text-green-400",   rowBg: "bg-green-500/5" },
  pr_reviewed_fail:   { dot: "bg-amber-400",            label: "Changes Needed", labelColor: "text-amber-400",   rowBg: "bg-amber-500/5" },
  merged:             { dot: "bg-purple-400",           label: "Merged",         labelColor: "text-purple-400" },
  closed:             { dot: "bg-muted-foreground/30",  label: "Closed",         labelColor: "text-muted-foreground" },
};

function timeAgo(iso: string): string {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export function IssueCard({ issue, onReview, onAssignCopilot, onImplementAider, isReviewing, isAssigning, isImplementing }: IssueCardProps) {
  const [reviewExpanded, setReviewExpanded] = useState(false);
  const vs = toVisualState(issue);
  const meta = STATE_META[vs];
  const isDone = vs === "merged" || vs === "closed";
  const review = issue.pr_review?.summary ? issue.pr_review : null;

  return (
    <div className={`px-4 py-3 transition-colors ${meta.rowBg ?? ""} ${isDone ? "opacity-50" : ""}`}>
      <div className="flex items-start gap-3">

        {/* Status dot */}
        <div className="mt-1.5 shrink-0">
          <div className={`h-2 w-2 rounded-full ${meta.dot}`} />
        </div>

        <div className="min-w-0 flex-1 space-y-1.5">
          {/* Title row */}
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline gap-2 flex-wrap">
                <span className="shrink-0 font-mono text-xs text-muted-foreground">#{issue.issue_number}</span>
                <span className={`text-sm leading-snug ${isDone ? "line-through text-muted-foreground" : "font-medium"}`}>
                  {issue.title}
                </span>
              </div>
              {issue.story && (
                <p className="mt-0.5 truncate text-xs text-muted-foreground">{issue.story}</p>
              )}
            </div>
            {/* Status label */}
            <span className={`shrink-0 text-xs font-semibold ${meta.labelColor}`}>
              {meta.label}
            </span>
          </div>

          {/* Action row */}
          <div className="flex flex-wrap items-center gap-2">

            {/* Issue link */}
            {issue.copilot_workspace_url && (
              <a href={issue.copilot_workspace_url} target="_blank" rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
                <ExternalLink className="h-3 w-3" /> Issue #{issue.issue_number}
              </a>
            )}

            {/* PR link */}
            {issue.pr_url && (
              <a href={issue.pr_url} target="_blank" rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 rounded-md border border-blue-500/30 bg-blue-500/5 px-2 py-1 text-xs font-medium text-blue-400 hover:bg-blue-500/15 transition-colors">
                <GitPullRequest className="h-3 w-3" />
                PR #{issue.pr_number}
                <ExternalLink className="h-2.5 w-2.5" />
              </a>
            )}

            {/* Assign Copilot — open, no PR */}
            {vs === "open" && (
              <button onClick={() => onAssignCopilot(issue.issue_number)} disabled={isAssigning || isImplementing}
                className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/5 px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10 transition-colors disabled:opacity-50">
                {isAssigning ? <Loader2 className="h-3 w-3 animate-spin" /> : <Zap className="h-3 w-3" />}
                {isAssigning ? "Assigning…" : "Assign Copilot"}
              </button>
            )}

            {/* Implement with Aider — open, no PR */}
            {vs === "open" && (
              <button onClick={() => onImplementAider(issue.issue_number)} disabled={isImplementing || isAssigning}
                className="inline-flex items-center gap-1.5 rounded-md border border-violet-500/40 bg-violet-500/5 px-2 py-1 text-xs font-medium text-violet-400 hover:bg-violet-500/10 transition-colors disabled:opacity-50">
                {isImplementing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Bot className="h-3 w-3" />}
                {isImplementing ? "Implementing…" : "Implement"}
              </button>
            )}

            {/* Request AI Review — when PR is open */}
            {vs === "pr_open" && issue.pr_number && (
              <button onClick={() => onReview(issue.issue_number, issue.pr_number!)} disabled={isReviewing}
                className="inline-flex items-center gap-1.5 rounded-md bg-blue-500 px-2.5 py-1 text-xs font-semibold text-white hover:bg-blue-600 transition-colors disabled:opacity-50 shadow-sm">
                {isReviewing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Eye className="h-3 w-3" />}
                {isReviewing ? "Reviewing…" : "Request AI Review"}
              </button>
            )}

            {/* Re-review after suggestions */}
            {vs === "pr_reviewed_fail" && issue.pr_number && (
              <button onClick={() => onReview(issue.issue_number, issue.pr_number!)} disabled={isReviewing}
                className="inline-flex items-center gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/5 px-2 py-1 text-xs font-medium text-amber-400 hover:bg-amber-500/10 transition-colors disabled:opacity-50">
                {isReviewing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Eye className="h-3 w-3" />}
                Re-review
              </button>
            )}

            {/* Approve PR — shown when review passed or when there are suggestions */}
            {(vs === "pr_reviewed_pass" || vs === "pr_reviewed_fail") && issue.pr_url && (
              <a href={issue.pr_url} target="_blank" rel="noopener noreferrer"
                className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-semibold transition-colors shadow-sm
                  ${vs === "pr_reviewed_pass"
                    ? "bg-green-500 text-white hover:bg-green-600"
                    : "border border-amber-500/40 bg-amber-500/5 text-amber-400 hover:bg-amber-500/10"}`}>
                <ThumbsUp className="h-3 w-3" />
                {vs === "pr_reviewed_pass" ? "Approve & Merge" : "Approve Anyway"}
                <ExternalLink className="h-2.5 w-2.5" />
              </a>
            )}

            {/* Timestamp */}
            <span className="ml-auto text-xs text-muted-foreground/40">{timeAgo(issue.updated_at)}</span>
          </div>
        </div>
      </div>

      {/* Review result */}
      {review && (
        <div className="ml-5 mt-2 rounded-lg border border-border bg-card px-3 py-2">
          <button onClick={() => setReviewExpanded((v) => !v)}
            className="flex w-full items-center justify-between gap-2 text-xs">
            <span className={`flex items-center gap-1.5 font-medium ${review.passed ? "text-green-400" : "text-amber-400"}`}>
              {review.passed
                ? <CheckCircle2 className="h-3.5 w-3.5" />
                : <XCircle className="h-3.5 w-3.5" />}
              {review.passed ? "AI review passed" : "AI review — changes needed"}
            </span>
            {reviewExpanded ? <ChevronUp className="h-3 w-3 text-muted-foreground" /> : <ChevronDown className="h-3 w-3 text-muted-foreground" />}
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
