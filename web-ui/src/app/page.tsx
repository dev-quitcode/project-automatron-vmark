"use client";

import { useEffect, useState } from "react";
import { AppLayout } from "@/components/layout";
import { ProjectCard } from "@/components/project/ProjectCard";
import { NewProjectDialog } from "@/components/project/NewProjectDialog";
import { useProjectStore } from "@/stores/projectStore";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Plus, FolderKanban } from "lucide-react";
import { deleteProject as apiDeleteProject } from "@/lib/api";

export default function DashboardPage() {
  const [dialogOpen, setDialogOpen] = useState(false);
  const { projects, isLoading, fetchProjects, setProjects, setCurrentProject } =
    useProjectStore();

  // Connect WebSocket (no specific project)
  useWebSocket();

  // Clear current project on dashboard
  useEffect(() => {
    setCurrentProject(null);
    fetchProjects();
  }, []);

  const handleDelete = async (id: string) => {
    try {
      await apiDeleteProject(id);
      setProjects(projects.filter((p) => p.id !== id));
    } catch {
      // handled by store
    }
  };

  return (
    <AppLayout>
      {/* Header */}
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Projects</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Manage your autonomous development projects
          </p>
        </div>
        <button
          onClick={() => setDialogOpen(true)}
          className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
        >
          <Plus className="h-4 w-4" />
          New Project
        </button>
      </div>

      {/* Project grid */}
      {isLoading ? (
        <div className="flex items-center justify-center py-20 text-muted-foreground">
          Loading projects...
        </div>
      ) : projects.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <FolderKanban className="mb-4 h-12 w-12 text-muted-foreground/40" />
          <h3 className="text-lg font-medium">No projects yet</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            Create your first project to get started with Automatron.
          </p>
          <button
            onClick={() => setDialogOpen(true)}
            className="mt-4 flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
          >
            <Plus className="h-4 w-4" />
            Create Project
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {projects.filter((p) => p.status !== "deleted").map((project) => (
            <ProjectCard
              key={project.id}
              project={project}
              onDelete={handleDelete}
            />
          ))}
        </div>
      )}

      <NewProjectDialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
      />
    </AppLayout>
  );
}
