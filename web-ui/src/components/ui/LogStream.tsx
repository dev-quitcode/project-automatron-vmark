"use client";

import { useRef, useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import type { BuilderLog, BuilderStatus } from "@/lib/types";
import {
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Info,
  Loader2,
  ChevronDown,
  ChevronUp,
  Zap,
} from "lucide-react";

interface LogStreamProps {
  logs: BuilderLog[];
  className?: string;
  maxHeight?: string;
}

const STATUS_META: Record<
  BuilderStatus,
  { icon: React.ReactNode; dot: string; label: string; textColor: string }
> = {
  SUCCESS: {
    icon: <CheckCircle2 className="h-4 w-4" />,
    dot: "bg-green-500",
    label: "Success",
    textColor: "text-green-500",
  },
  BLOCKER: {
    icon: <XCircle className="h-4 w-4" />,
    dot: "bg-red-500",
    label: "Blocker",
    textColor: "text-red-500",
  },
  ERROR: {
    icon: <XCircle className="h-4 w-4" />,
    dot: "bg-red-500",
    label: "Error",
    textColor: "text-red-500",
  },
  AMBIGUITY: {
    icon: <AlertTriangle className="h-4 w-4" />,
    dot: "bg-amber-500",
    label: "Warning",
    textColor: "text-amber-500",
  },
  SILENT_DECISION: {
    icon: <Zap className="h-4 w-4" />,
    dot: "bg-violet-500",
    label: "Decision",
    textColor: "text-violet-400",
  },
  INFO: {
    icon: <Info className="h-4 w-4" />,
    dot: "bg-blue-500",
    label: "Info",
    textColor: "text-blue-400",
  },
  RUNNING: {
    icon: <Loader2 className="h-4 w-4 animate-spin" />,
    dot: "bg-yellow-500 animate-pulse",
    label: "Running",
    textColor: "text-yellow-400",
  },
};

function timeAgo(timestamp: string): string {
  const diff = (Date.now() - new Date(timestamp).getTime()) / 1000;
  if (diff < 5) return "just now";
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

function ActivityEntry({ log }: { log: BuilderLog }) {
  const [expanded, setExpanded] = useState(false);
  const meta = STATUS_META[log.status] ?? STATUS_META.INFO;
  const hasOutput = Boolean(log.output?.trim());

  return (
    <div className="relative flex gap-3 pb-4 last:pb-0">
      {/* Timeline line */}
      <div className="absolute left-[7px] top-5 bottom-0 w-px bg-border last:hidden" />

      {/* Status dot */}
      <div className="relative mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center">
        <div className={cn("h-2 w-2 rounded-full", meta.dot)} />
      </div>

      {/* Content */}
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-1.5 min-w-0">
            <span className={cn("shrink-0", meta.textColor)}>{meta.icon}</span>
            <span className="text-sm font-medium leading-tight">{log.task_text}</span>
          </div>
          <span className="shrink-0 text-xs text-muted-foreground/60 mt-0.5">
            {timeAgo(log.timestamp)}
          </span>
        </div>

        {log.output?.trim() && (
          <div className="mt-1">
            {hasOutput && !expanded ? (
              <button
                onClick={() => setExpanded(true)}
                className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
              >
                <span className="truncate max-w-[360px]">{log.output.split("\n")[0]}</span>
                {log.output.includes("\n") && (
                  <ChevronDown className="h-3 w-3 shrink-0" />
                )}
              </button>
            ) : expanded ? (
              <div>
                <pre className="mt-1 whitespace-pre-wrap rounded bg-muted/40 px-3 py-2 text-xs text-muted-foreground leading-relaxed">
                  {log.output.slice(0, 2000)}
                </pre>
                <button
                  onClick={() => setExpanded(false)}
                  className="mt-1 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                >
                  <ChevronUp className="h-3 w-3" />
                  collapse
                </button>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground truncate">{log.output}</p>
            )}
          </div>
        )}

        {(log.status === "ERROR" || log.status === "BLOCKER") && log.error_detail && (
          <pre className="mt-1.5 whitespace-pre-wrap rounded border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs text-red-400 leading-relaxed">
            {log.error_detail.slice(0, 2000)}
          </pre>
        )}
      </div>
    </div>
  );
}

export function LogStream({ logs, className, maxHeight = "400px" }: LogStreamProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  // Newest at top — scroll to top when new logs arrive
  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = 0;
    }
  }, [logs.length]);

  if (logs.length === 0) {
    return (
      <div
        className={cn(
          "flex flex-col items-center justify-center rounded-xl border border-border bg-card p-12 text-sm text-muted-foreground",
          className
        )}
      >
        <Info className="mb-3 h-8 w-8 opacity-20" />
        <p className="font-medium">No activity yet</p>
        <p className="mt-1 text-xs opacity-70">Events will appear here as Automatron works.</p>
      </div>
    );
  }

  const successCount = logs.filter((l) => l.status === "SUCCESS").length;
  const errorCount = logs.filter((l) => l.status === "BLOCKER" || l.status === "ERROR").length;

  return (
    <div className={cn("flex flex-col gap-0 rounded-xl border border-border bg-card", className)}>
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <p className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
          Activity
        </p>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          {successCount > 0 && (
            <span className="text-green-500">{successCount} completed</span>
          )}
          {errorCount > 0 && (
            <span className="text-red-500">{errorCount} errors</span>
          )}
          <span>{logs.length} events</span>
        </div>
      </div>

      {/* Feed — newest first, sorted by timestamp so out-of-order WebSocket
          events and racy backend seq values still display chronologically. */}
      <div
        ref={containerRef}
        className="overflow-auto px-4 py-4"
        style={{ maxHeight }}
      >
        {[...logs]
          .sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime())
          .map((log, i) => (
            <ActivityEntry key={`${log.task_index}-${i}`} log={log} />
          ))}
      </div>
    </div>
  );
}
