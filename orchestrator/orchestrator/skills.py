"""Fetch and cache stack-specific best-practice skills from skills.sh / GitHub.

Each skill is a `SKILL.md` markdown file hosted in a public GitHub repo. We detect
which skills are relevant to the target project based on its `package.json` and
related config files, fetch the matching `SKILL.md`s (cached locally), and let the
caller inject the content into the architect / reviewer / builder prompts.

Designed to be best-effort: any network or parse failure logs a warning and
returns empty context — the orchestrator should continue working without skills.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from orchestrator.config import settings

logger = logging.getLogger(__name__)

# Cap each skill's contribution to the prompt so we don't blow the token budget
# when many skills are loaded at once.
_PER_SKILL_CHAR_LIMIT = 6000

# Catalog: id → GitHub raw URL of the SKILL.md
# IDs are stable, human-readable, and used as cache filenames.
SKILL_SOURCES: dict[str, str] = {
    "next-best-practices":
        "https://raw.githubusercontent.com/vercel-labs/next-skills/main/skills/next-best-practices/SKILL.md",
    "react-best-practices":
        "https://raw.githubusercontent.com/vercel-labs/agent-skills/main/skills/react-best-practices/SKILL.md",
    "web-design-guidelines":
        "https://raw.githubusercontent.com/vercel-labs/agent-skills/main/skills/web-design-guidelines/SKILL.md",
    "supabase-postgres-best-practices":
        "https://raw.githubusercontent.com/supabase/agent-skills/main/skills/supabase-postgres-best-practices/SKILL.md",
    "supabase":
        "https://raw.githubusercontent.com/supabase/agent-skills/main/skills/supabase/SKILL.md",
    "tdd":
        "https://raw.githubusercontent.com/mattpocock/skills/main/skills/engineering/tdd/SKILL.md",
    "improve-codebase-architecture":
        "https://raw.githubusercontent.com/mattpocock/skills/main/skills/engineering/improve-codebase-architecture/SKILL.md",
}


def _cache_dir() -> Path:
    """Cache directory, sibling of the sqlite db (e.g. data/skills_cache/)."""
    d = settings.sqlite_db_dir / "skills_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def detect_skills(package_json: str, tailwind_config: str, components_json: str) -> list[str]:
    """Return skill IDs relevant to a project based on its stack files.

    Inputs are raw text content (already fetched from the target repo).
    """
    skill_ids: list[str] = []
    pkg = package_json or ""
    tw = tailwind_config or ""
    comp = components_json or ""

    # next must come before react (Next.js implies React but the Next skill is more specific)
    if '"next"' in pkg or "'next'" in pkg:
        skill_ids.append("next-best-practices")
    if '"react"' in pkg or "'react'" in pkg:
        skill_ids.append("react-best-practices")
    if "@supabase/supabase-js" in pkg or "supabase" in pkg.lower():
        skill_ids.append("supabase-postgres-best-practices")
        skill_ids.append("supabase")
    if tw.strip() or comp.strip() or "shadcn" in pkg.lower() or "tailwindcss" in pkg:
        skill_ids.append("web-design-guidelines")
    # Always-on skills — apply to any project
    skill_ids.append("tdd")
    skill_ids.append("improve-codebase-architecture")

    # Dedupe preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for sid in skill_ids:
        if sid in SKILL_SOURCES and sid not in seen:
            seen.add(sid)
            deduped.append(sid)
    return deduped


async def load_skill(skill_id: str) -> str:
    """Return the SKILL.md content for a given id. Cached. Returns '' on failure."""
    if skill_id not in SKILL_SOURCES:
        return ""

    cache_path = _cache_dir() / f"{skill_id}.md"
    if cache_path.exists():
        try:
            return cache_path.read_text(errors="replace")
        except OSError as exc:
            logger.warning("skills: cache read failed for %s: %s", skill_id, exc)

    url = SKILL_SOURCES[skill_id]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, follow_redirects=True)
    except Exception as exc:
        logger.warning("skills: fetch failed for %s (%s): %s", skill_id, url, exc)
        return ""

    if resp.status_code != 200:
        logger.warning("skills: fetch returned HTTP %d for %s", resp.status_code, skill_id)
        return ""

    text = resp.text or ""
    try:
        cache_path.write_text(text)
    except OSError as exc:
        logger.warning("skills: cache write failed for %s: %s", skill_id, exc)
    return text


async def build_skill_context(skill_ids: list[str], section_title: str) -> str:
    """Format the given skills as a prompt-ready markdown section. Returns '' on no skills."""
    if not skill_ids:
        return ""

    bodies = await asyncio.gather(*(load_skill(sid) for sid in skill_ids))
    parts: list[str] = []
    for sid, body in zip(skill_ids, bodies):
        if not body:
            continue
        trimmed = body[:_PER_SKILL_CHAR_LIMIT]
        if len(body) > _PER_SKILL_CHAR_LIMIT:
            trimmed += f"\n\n[...truncated at {_PER_SKILL_CHAR_LIMIT} chars]"
        parts.append(f"### Skill: `{sid}`\n\n{trimmed}")

    if not parts:
        return ""

    return f"## {section_title}\n\n" + "\n\n---\n\n".join(parts)
