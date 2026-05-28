"use client";

import { useCallback, useEffect, useRef } from "react";
import * as api from "@/lib/api";
import {
  connectSocket,
  disconnectSocket,
  getSocket,
  joinProjectRoom,
  leaveProjectRoom,
  resetSocket,
} from "@/lib/socket";
import { useProjectStore } from "@/stores/projectStore";
import type {
  BuilderLog,
  ChatMessage,
  GithubIssue,
  PRReview,
  WsArchitectMessage,
  WsBuilderLog,
  WsHumanRequired,
  WsPlanUpdated,
  WsStatusUpdate,
} from "@/lib/types";

export function useWebSocket(projectId?: string) {
  const previousProjectId = useRef<string | null>(null);
  const {
    addBuilderLog,
    addChatMessage,
    patchProject,
    setConnected,
    setHumanRequired,
    setPlanMd,
    setProgress,
    setIssues,
    updateIssue,
    setBuildFailure,
    setBuildPassed,
    fetchChatHistory,
    fetchLogs,
    fetchPlan,
  } = useProjectStore();

  useEffect(() => {
    const socket = connectSocket();

    socket.on("connect", () => {
      setConnected(true);
      if (projectId) {
        joinProjectRoom(projectId);
        // Refetch everything WebSocket events would have delivered while
        // disconnected — issues, chat, activity log, plan. Fire-and-forget;
        // store actions handle their own errors.
        api.getIssues(projectId).then(setIssues).catch(() => {});
        void fetchChatHistory(projectId);
        void fetchLogs(projectId);
        void fetchPlan(projectId);
      }
    });

    socket.on("disconnect", () => {
      setConnected(false);
    });

    socket.on("architect:message", (data: WsArchitectMessage) => {
      if (projectId && data.project_id !== projectId) {
        return;
      }
      if (data.is_streaming) {
        return;
      }
      const message: ChatMessage = {
        id: crypto.randomUUID(),
        project_id: data.project_id,
        role: "architect",
        content: data.content,
        timestamp: new Date().toISOString(),
      };
      addChatMessage(message);
    });

    socket.on("builder:log", (data: WsBuilderLog) => {
      if (projectId && data.project_id !== projectId) {
        return;
      }
      const log: BuilderLog = {
        project_id: data.project_id,
        task_index: data.task_index,
        task_text: data.task_text,
        status: data.status,
        output: data.output,
        error_detail: null,
        timestamp: new Date().toISOString(),
      };
      addBuilderLog(log);
    });

    socket.on("status:update", (data: WsStatusUpdate) => {
      patchProject(data.project_id, {
        status: data.status,
        project_stage: data.stage,
        preview_url: data.preview_url ?? null,
      });

      if (!projectId || data.project_id === projectId) {
        const total = data.progress?.total ?? 0;
        const completed = data.progress?.completed ?? 0;
        setProgress({
          total,
          completed,
          percentage: total > 0 ? Math.round((completed / total) * 100) : 0,
        });
      }
    });

    socket.on("human:required", (data: WsHumanRequired) => {
      if (!projectId || data.project_id === projectId) {
        setHumanRequired(true, data.reason, data.stage ?? null);
      }
      if (data.stage) {
        patchProject(data.project_id, { project_stage: data.stage });
      }
    });

    socket.on("plan:updated", (data: WsPlanUpdated) => {
      patchProject(data.project_id, { plan_md: data.plan_md });
      if (!projectId || data.project_id === projectId) {
        setPlanMd(data.plan_md);
      }
    });

    socket.on("run:error", (data: { project_id: string; message: string; stage: string }) => {
      if (projectId && data.project_id !== projectId) return;
      const log: BuilderLog = {
        project_id: data.project_id,
        task_index: -1,
        task_text: "Orchestrator Error",
        status: "ERROR",
        output: data.message,
        error_detail: data.message,
        timestamp: new Date().toISOString(),
      };
      addBuilderLog(log);
    });

    socket.on("issues:updated", (data: { project_id: string; issues: GithubIssue[] }) => {
      if (projectId && data.project_id !== projectId) return;
      // Play notification when a new PR_open appears (Copilot opened a PR)
      const prev = useProjectStore.getState().issues;
      const prevPrOpen = new Set(prev.filter((i) => i.status === "pr_open").map((i) => i.issue_number));
      const newPrOpen = data.issues.filter((i) => i.status === "pr_open" && !prevPrOpen.has(i.issue_number));
      if (newPrOpen.length > 0) {
        try {
          const audio = new Audio("/notification.mp3");
          audio.volume = 1.0;
          audio.play().catch(() => {});
        } catch {}
      }
      setIssues(data.issues);
    });

    socket.on(
      "pr:review_ready",
      (data: { project_id: string; issue_number: number; pr_number: number; passed: boolean; summary: string }) => {
        if (projectId && data.project_id !== projectId) return;
        const { issues } = useProjectStore.getState();
        const issue = issues.find((i) => i.issue_number === data.issue_number);
        if (issue) {
          updateIssue({
            ...issue,
            status: "pr_reviewed",
            pr_review: { passed: data.passed, summary: data.summary, pr_number: data.pr_number, issue_number: data.issue_number },
          });
        }
      }
    );

    socket.on(
      "build:failed",
      (data: { project_id: string; error_summary: string; default_branch: string }) => {
        if (projectId && data.project_id !== projectId) return;
        setBuildFailure({ errorSummary: data.error_summary, defaultBranch: data.default_branch });
      }
    );

    socket.on(
      "build:passed",
      (data: { project_id: string; default_branch: string }) => {
        if (projectId && data.project_id !== projectId) return;
        setBuildPassed(data.default_branch);
        setTimeout(() => setBuildPassed(null), 6000);
      }
    );

    socket.on(
      "aider:needs_help",
      (data: { project_id: string; issue_number: number; error_summary: string }) => {
        if (projectId && data.project_id !== projectId) return;
        const log: BuilderLog = {
          project_id: data.project_id,
          task_index: -1,
          task_text: `Aider needs help on #${data.issue_number}`,
          status: "ERROR",
          output: `Pre-push build failed on this PR but main is clean. Aider's branch was NOT pushed.\n\n${data.error_summary}`,
          error_detail: data.error_summary,
          timestamp: new Date().toISOString(),
        };
        addBuilderLog(log);
      }
    );

    return () => {
      socket.off("connect");
      socket.off("disconnect");
      socket.off("architect:message");
      socket.off("builder:log");
      socket.off("status:update");
      socket.off("human:required");
      socket.off("plan:updated");
      socket.off("run:error");
      socket.off("issues:updated");
      socket.off("pr:review_ready");
      socket.off("build:failed");
      socket.off("build:passed");
      socket.off("aider:needs_help");
      // Fully reset so the next mount (or projectId change) gets a fresh
      // socket without stale handlers or room subscriptions.
      resetSocket();
    };
  }, [
    addBuilderLog,
    addChatMessage,
    patchProject,
    projectId,
    setConnected,
    setHumanRequired,
    setIssues,
    setPlanMd,
    setProgress,
    updateIssue,
    setBuildFailure,
    setBuildPassed,
    fetchChatHistory,
    fetchLogs,
    fetchPlan,
  ]);

  useEffect(() => {
    if (previousProjectId.current && previousProjectId.current !== projectId) {
      leaveProjectRoom(previousProjectId.current);
    }

    if (projectId) {
      joinProjectRoom(projectId);
    }
    previousProjectId.current = projectId || null;

    return () => {
      if (projectId) {
        leaveProjectRoom(projectId);
      }
    };
  }, [projectId]);

  const sendMessage = useCallback(
    (message: string) => {
      if (!projectId) return;

      const socket = getSocket();
      socket.emit("chat:message", { project_id: projectId, message });

      const optimisticMessage: ChatMessage = {
        id: crypto.randomUUID(),
        project_id: projectId,
        role: "user",
        content: message,
        timestamp: new Date().toISOString(),
      };
      addChatMessage(optimisticMessage);
    },
    [addChatMessage, projectId]
  );

  return { sendMessage };
}
