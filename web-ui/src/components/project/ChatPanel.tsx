"use client";

import { useState, useRef, useEffect } from "react";
import { cn } from "@/lib/utils";
import { Send, Bot, User, Cpu } from "lucide-react";
import type { ChatMessage } from "@/lib/types";
import ReactMarkdown from "react-markdown";

interface ChatPanelProps {
  messages: ChatMessage[];
  onSendMessage: (message: string) => void;
  disabled?: boolean;
  placeholder?: string;
}

export function ChatPanel({
  messages,
  onSendMessage,
  disabled = false,
  placeholder,
}: ChatPanelProps) {
  const [input, setInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = () => {
    const trimmed = input.trim();
    if (!trimmed || disabled) return;
    onSendMessage(trimmed);
    setInput("");
    inputRef.current?.focus();
  };

  const getRoleIcon = (role: string) => {
    switch (role) {
      case "user":
        return <User className="h-4 w-4" />;
      case "architect":
        return <Bot className="h-4 w-4" />;
      case "system":
        return <Cpu className="h-4 w-4" />;
      default:
        return <Bot className="h-4 w-4" />;
    }
  };

  const getRoleLabel = (role: string) => {
    switch (role) {
      case "user":
        return "You";
      case "architect":
        return "Architect";
      case "system":
        return "System";
      default:
        return role;
    }
  };

  return (
    <div className="flex h-full flex-col rounded-xl border border-border bg-card">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <Bot className="h-4 w-4 text-primary" />
        <h3 className="text-sm font-semibold">Architect Chat</h3>
      </div>

      {/* Messages */}
      <div className="flex-1 space-y-4 overflow-auto p-4">
        {messages.length === 0 && (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            Start the project to begin a conversation with the Architect.
          </div>
        )}
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={cn(
              "flex gap-3",
              msg.role === "user" ? "justify-end" : "justify-start"
            )}
          >
            {msg.role !== "user" && (
              <div
                className={cn(
                  "flex h-7 w-7 shrink-0 items-center justify-center rounded-full",
                  msg.role === "architect"
                    ? "bg-primary/10 text-primary"
                    : "bg-muted text-muted-foreground"
                )}
              >
                {getRoleIcon(msg.role)}
              </div>
            )}
            <div
              className={cn(
                "max-w-[80%] rounded-xl px-4 py-2.5 text-sm",
                msg.role === "user"
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted"
              )}
            >
              <div className="mb-1 text-xs font-medium opacity-70">
                {getRoleLabel(msg.role)}
              </div>
              <div className="prose prose-sm dark:prose-invert max-w-none">
                <ReactMarkdown>{msg.content}</ReactMarkdown>
              </div>
            </div>
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="border-t border-border p-4">
        <div className="flex gap-2">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder={
              disabled
                ? "Chat disabled while processing..."
                : placeholder ?? "Message the Architect..."
            }
            disabled={disabled}
            className="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
          />
          <button
            onClick={handleSend}
            disabled={!input.trim() || disabled}
            className="flex items-center justify-center rounded-lg bg-primary p-2.5 text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
          >
            <Send className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
