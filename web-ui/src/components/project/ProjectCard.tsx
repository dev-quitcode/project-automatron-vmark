"use client";

import Link from "next/link";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { Project } from "@/lib/types";
import { ArrowRight, Trash2, GitBranch, ExternalLink } from "lucide-react";

interface ProjectCardProps {
  project: Project;
  onDelete?: (id: string) => void;
}

function timeAgo(iso: string): string {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const STAGE_LABEL: Record<string, string> = {
  intake: "Intake",
  awaiting_plan_approval: "Awaiting approval",
  planning: "Planning",
  building: "Building",
  awaiting_preview_approval: "Preview ready",
  ready_for_deploy: "Ready to deploy",
  deployed: "Deployed",
};

export function ProjectCard({ project, onDelete }: ProjectCardProps) {
  const repoUrl = project.repo_url?.replace(/\.git$/, "") ?? null;
  const stageLabel = STAGE_LABEL[project.project_stage] ?? project.project_stage.replace(/_/g, " ");

  return (
    <div className="group relative rounded-xl border border-border bg-card p-5 transition-colors hover:border-primary/30">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate font-semibold">{project.name}</h3>
          </div>
          <p className="text-sm text-muted-foreground line-clamp-2">
            {project.description}
          </p>
          <div className="flex flex-wrap gap-1.5 pt-1">
            <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
              {stageLabel}
            </span>
            {repoUrl && (
              <a
                href={repoUrl}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
              >
                <GitBranch className="h-3 w-3" />
                {repoUrl.split("/").slice(-1)[0]}
                <ExternalLink className="h-2.5 w-2.5" />
              </a>
            )}
          </div>
        </div>
        <StatusBadge status={project.status} />
      </div>

      <div className="mt-4 flex items-center justify-between text-xs text-muted-foreground">
        <span>Active {timeAgo(project.updated_at)}</span>
        <div className="flex items-center gap-2">
          {onDelete && (
            <button
              onClick={(e) => { e.preventDefault(); onDelete(project.id); }}
              className="rounded p-1 opacity-0 transition-opacity hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}
          <Link
            href={`/project/${project.id}`}
            className="flex items-center gap-1 text-primary hover:underline"
          >
            Open
            <ArrowRight className="h-3 w-3" />
          </Link>
        </div>
      </div>
    </div>
  );
}
