"use client";

import { useState } from "react";
import type { GithubIssue, IssueStatus } from "@/lib/types";
import {
  ExternalLink,
  GitPullRequest,
  CheckCircle2,
  Circle,
  GitMerge,
  Loader2,
  ChevronDown,
  ChevronUp,
} from "lucide-react";

interface IssueCardProps {
  issue: GithubIssue;
  onReview: (issueNumber: number, prNumber: number) => void;
  onAssignCopilot: (issueNumber: number) => void;
  isReviewing: boolean;
  isAssigning: boolean;
}

const STATUS_CONFIG: Record<IssueStatus, { label: string; color: string; icon: React.ReactNode }> = {
  open: {
    label: "Open",
    color: "text-muted-foreground bg-muted",
    icon: <Circle className="h-3.5 w-3.5" />,
  },
  pr_open: {
    label: "PR open",
    color: "text-blue-400 bg-blue-400/10",
    icon: <GitPullRequest className="h-3.5 w-3.5" />,
  },
  pr_reviewed: {
    label: "Reviewed",
    color: "text-amber-400 bg-amber-400/10",
    icon: <GitPullRequest className="h-3.5 w-3.5" />,
  },
  merged: {
    label: "Merged",
    color: "text-purple-400 bg-purple-400/10",
    icon: <GitMerge className="h-3.5 w-3.5" />,
  },
  closed: {
    label: "Closed",
    color: "text-green-400 bg-green-400/10",
    icon: <CheckCircle2 className="h-3.5 w-3.5" />,
  },
};

export function IssueCard({ issue, onReview, onAssignCopilot, isReviewing, isAssigning }: IssueCardProps) {
  const [reviewExpanded, setReviewExpanded] = useState(false);
  const cfg = STATUS_CONFIG[issue.status] ?? STATUS_CONFIG.open;
  const isDone = issue.status === "merged" || issue.status === "closed";

  // pr_review comes back as {} when no review exists — treat that as null
  const review = issue.pr_review?.summary ? issue.pr_review : null;

  return (
    <div className={`rounded-lg border border-border bg-card px-4 py-3 transition-opacity ${isDone ? "opacity-60" : ""}`}>
      <div className="flex items-start gap-3">
        {/* Issue number */}
        <span className="mt-0.5 shrink-0 text-xs text-muted-foreground font-mono">#{issue.issue_number}</span>

        {/* Title + badges */}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`text-sm ${isDone ? "line-through text-muted-foreground" : "font-medium"}`}>
              {issue.title}
            </span>
            <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${cfg.color}`}>
              {cfg.icon}
              {cfg.label}
            </span>
          </div>
          {issue.story && (
            <p className="mt-0.5 text-xs text-muted-foreground">{issue.story}</p>
          )}
        </div>

        {/* Action buttons */}
        <div className="shrink-0 flex items-center gap-2">

          {/* AI Review button — shown when PR is open */}
          {issue.status === "pr_open" && issue.pr_number && (
            <button
              onClick={() => onReview(issue.issue_number, issue.pr_number!)}
              disabled={isReviewing}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-muted disabled:opacity-50"
            >
              {isReviewing ? <Loader2 className="h-3 w-3 animate-spin" /> : <GitPullRequest className="h-3 w-3" />}
              Review PR
            </button>
          )}

          {/* Open PR button — shown whenever a PR exists */}
          {issue.pr_url && (
            <a
              href={issue.pr_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-md border border-blue-500/30 bg-blue-500/5 px-2.5 py-1.5 text-xs font-medium text-blue-400 transition-colors hover:bg-blue-500/10"
            >
              <GitPullRequest className="h-3 w-3" />
              Open PR #{issue.pr_number}
              <ExternalLink className="h-3 w-3" />
            </a>
          )}

          {/* Assign Copilot button — only when open with no PR yet */}
          {issue.status === "open" && !issue.pr_url && (
            <button
              onClick={() => onAssignCopilot(issue.issue_number)}
              disabled={isAssigning}
              className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/5 px-2.5 py-1.5 text-xs font-medium text-primary transition-colors hover:bg-primary/10 disabled:opacity-50"
            >
              {isAssigning ? <Loader2 className="h-3 w-3 animate-spin" /> : <GitPullRequest className="h-3 w-3" />}
              Assign Copilot
            </button>
          )}

          {/* View on GitHub — shown for non-open issues with a workspace URL */}
          {issue.status !== "open" && issue.copilot_workspace_url && (
            <a
              href={issue.copilot_workspace_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
            >
              View on GitHub
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
      </div>

      {/* PR review result */}
      {review && (
        <div className="mt-2 border-t border-border pt-2">
          <button
            onClick={() => setReviewExpanded((v) => !v)}
            className="flex w-full items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
          >
            <span className={review.passed ? "text-green-400" : "text-amber-400"}>
              {review.passed ? "✅ Review passed" : "⚠️ Issues found"}
            </span>
            {reviewExpanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>

          {reviewExpanded && (
            <pre className="mt-2 whitespace-pre-wrap rounded bg-muted/50 p-2 text-xs text-muted-foreground leading-relaxed">
              {review.summary}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
