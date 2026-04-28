"""Deterministic stack detection for child repos.

Slice 1 supports only Next.js + npm. Other package managers and frameworks
return `framework="unsupported"` / `package_manager="unsupported"` so the
strategy can surface a blocking preflight check instead of silently guessing.

The detector is pure: it takes a `path -> content` snapshot and returns a
plain dict. Callers fetch the file contents via the GitHub Contents API.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

PROBE_FILES: tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "next.config.cjs",
    "app/api/health/route.ts",
    "app/api/health/route.js",
    "src/app/api/health/route.ts",
    "src/app/api/health/route.js",
    "pages/api/health.ts",
    "pages/api/health.js",
    "pages/api/health/index.ts",
    "pages/api/health/index.js",
    "src/index.ts",
    "src/app/layout.tsx",
    "src/pages/index.tsx",
    "app/layout.tsx",
    "pages/index.tsx",
    "pages/api/_health.ts",
)

HEALTH_ROUTE_CANDIDATES: tuple[str, ...] = (
    "app/api/health/route.ts",
    "app/api/health/route.js",
    "src/app/api/health/route.ts",
    "src/app/api/health/route.js",
    "pages/api/health.ts",
    "pages/api/health.js",
    "pages/api/health/index.ts",
    "pages/api/health/index.js",
)

_STANDALONE_RE = re.compile(
    r"""output\s*[:=]\s*['"]standalone['"]""",
    re.IGNORECASE,
)


@dataclass
class StackDetection:
    framework: str = "unsupported"
    package_manager: str = "unsupported"
    router_style: str = "unknown"
    src_layout: bool = False
    next_output: str = "unknown"
    health_route_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "framework": self.framework,
            "package_manager": self.package_manager,
            "router_style": self.router_style,
            "src_layout": self.src_layout,
            "next_output": self.next_output,
            "health_route_files": list(self.health_route_files),
        }


class StackDetector:
    """Detects framework, package manager, router style, and health routes."""

    def detect(self, repo_files: dict[str, str | None]) -> StackDetection:
        """Returns a `StackDetection` describing the repo.

        `repo_files` maps `path -> content`. Missing files map to `None`.
        Paths absent from the dict are treated as missing.
        """
        result = StackDetection()

        package_json_text = repo_files.get("package.json")
        if package_json_text:
            result.framework = self._detect_framework(package_json_text)

        result.package_manager = self._detect_package_manager(repo_files)
        result.router_style, result.src_layout = self._detect_router(repo_files)
        result.next_output = self._detect_next_output(repo_files)
        result.health_route_files = self._detect_health_routes(repo_files)
        return result

    def _detect_framework(self, package_json_text: str) -> str:
        try:
            pkg = json.loads(package_json_text)
        except json.JSONDecodeError:
            return "unsupported"
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        if "next" in deps:
            return "nextjs"
        return "unsupported"

    def _detect_package_manager(self, repo_files: dict[str, str | None]) -> str:
        if repo_files.get("package-lock.json") is not None:
            return "npm"
        if repo_files.get("pnpm-lock.yaml") is not None:
            return "pnpm"
        if repo_files.get("yarn.lock") is not None:
            return "yarn"
        if repo_files.get("bun.lockb") is not None:
            return "bun"
        if repo_files.get("package.json") is not None:
            # No lockfile committed — treat as npm so workflows still work,
            # but mark it as the conservative default.
            return "npm"
        return "unsupported"

    def _detect_router(self, repo_files: dict[str, str | None]) -> tuple[str, bool]:
        has_app_top = any(p.startswith("app/") for p in repo_files if repo_files.get(p) is not None)
        has_app_src = any(
            p.startswith("src/app/") for p in repo_files if repo_files.get(p) is not None
        )
        has_pages = any(
            p.startswith("pages/api") or p == "pages/index.tsx"
            for p in repo_files
            if repo_files.get(p) is not None
        )
        has_pages_src = any(
            p.startswith("src/pages") for p in repo_files if repo_files.get(p) is not None
        )
        src_layout = has_app_src or has_pages_src or any(
            p.startswith("src/") for p in repo_files if repo_files.get(p) is not None
        )
        if has_app_top or has_app_src:
            return "app", src_layout
        if has_pages or has_pages_src:
            return "pages", src_layout
        return "unknown", src_layout

    def _detect_next_output(self, repo_files: dict[str, str | None]) -> str:
        candidates = (
            "next.config.js",
            "next.config.mjs",
            "next.config.ts",
            "next.config.cjs",
        )
        for name in candidates:
            content = repo_files.get(name)
            if not content:
                continue
            if _STANDALONE_RE.search(content):
                return "standalone"
            return "default"
        return "unknown"

    def _detect_health_routes(self, repo_files: dict[str, str | None]) -> list[str]:
        return [path for path in HEALTH_ROUTE_CANDIDATES if repo_files.get(path)]


def is_supported(detection: StackDetection) -> bool:
    return detection.framework == "nextjs" and detection.package_manager == "npm"
