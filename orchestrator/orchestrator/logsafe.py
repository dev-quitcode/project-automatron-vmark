"""Redact secrets from text before logging.

Anything that gets passed to `logger.info`/`logger.error` that includes the
output of git, npm, docker, or Aider should be run through `redact()` first.
External command output is the most common path for tokens to leak into logs.
"""
from __future__ import annotations

import re

# Order matters: longer / more-specific patterns first
_REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # GitHub token embedded in clone URL (https://x-access-token:TOKEN@github.com/...)
    (re.compile(r"x-access-token:[^@\s]+@"), "x-access-token:***@"),
    # Generic Basic-auth-style URL credential (https://user:pass@host/...)
    (re.compile(r"(https?://)[^:/@\s]+:[^@\s]+@"), r"\1***:***@"),
    # Anthropic API keys (sk-ant-...)
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-***"),
    # OpenAI API keys (sk-proj-... or sk-...)
    (re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{30,}"), "sk-***"),
    # Google API keys (AIza...)
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "AIza***"),
    # GitHub tokens (ghp_, gho_, ghs_, ghu_, github_pat_)
    (re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}"), "gh*_***"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{50,}"), "github_pat_***"),
    # Supabase service role / anon keys (base64 JWTs starting with eyJ...)
    (re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"), "eyJ***"),
]


def redact(text: object) -> str:
    """Return `text` with known secret patterns replaced. Safe on non-str input."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    for pattern, replacement in _REDACTION_PATTERNS:
        s = pattern.sub(replacement, s)
    return s
