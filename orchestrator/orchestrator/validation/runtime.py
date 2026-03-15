"""Runtime specs for previewable generated projects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PreviewRuntimeSpec:
    """Deterministic preview/runtime contract for a generated workspace."""

    stack: str
    package_manager: str
    install_command: str
    preview_command_template: str
    cache_dirs: tuple[str, ...]
    readiness_path: str
    build_command: str | None = None
    prisma_smoke_command: str | None = None

    def render_preview_command(self, internal_port: int) -> str:
        return self.preview_command_template.format(port=internal_port)

    def probe_url(self, internal_port: int) -> str:
        return f"http://127.0.0.1:{internal_port}{self.readiness_path}"


def resolve_preview_runtime_spec(
    workspace_path: Path,
    stack_config: dict[str, Any] | None = None,
) -> PreviewRuntimeSpec:
    """Resolve a deterministic preview spec from workspace and stack metadata."""
    stack_text = str(stack_config or {}).lower()

    if (workspace_path / "next.config.js").exists() or (workspace_path / "next.config.ts").exists():
        package_manager = _detect_package_manager(workspace_path)
        return PreviewRuntimeSpec(
            stack="nextjs-prisma-sqlite-tailwind"
            if "prisma" in stack_text or (workspace_path / "prisma" / "schema.prisma").exists()
            else "nextjs",
            package_manager=package_manager,
            install_command=_install_command(package_manager),
            preview_command_template=_node_preview_command(package_manager, nextjs=True),
            cache_dirs=(".next",),
            readiness_path="/api/health",
            build_command=_node_build_command(package_manager),
            prisma_smoke_command=_prisma_smoke_command(),
        )

    if (workspace_path / "vite.config.ts").exists() or (workspace_path / "vite.config.js").exists():
        package_manager = _detect_package_manager(workspace_path)
        return PreviewRuntimeSpec(
            stack="vite",
            package_manager=package_manager,
            install_command=_install_command(package_manager),
            preview_command_template=_node_preview_command(package_manager, nextjs=False),
            cache_dirs=("dist",),
            readiness_path="/",
            build_command=_node_build_command(package_manager),
        )

    if (workspace_path / "pyproject.toml").exists() or (workspace_path / "requirements.txt").exists():
        return PreviewRuntimeSpec(
            stack="python",
            package_manager="python",
            install_command="python -m pip install -r requirements.txt" if (workspace_path / "requirements.txt").exists() else "true",
            preview_command_template="python -m http.server {port} --bind 0.0.0.0",
            cache_dirs=("__pycache__", ".pytest_cache"),
            readiness_path="/",
        )

    return PreviewRuntimeSpec(
        stack="static",
        package_manager="python",
        install_command="true",
        preview_command_template="python -m http.server {port} --bind 0.0.0.0",
        cache_dirs=(),
        readiness_path="/",
    )


def _detect_package_manager(workspace_path: Path) -> str:
    if (workspace_path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (workspace_path / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _install_command(package_manager: str) -> str:
    if package_manager == "pnpm":
        return "pnpm install"
    if package_manager == "yarn":
        return "yarn install --frozen-lockfile || yarn install"
    if package_manager == "npm":
        return "npm ci || npm install"
    return "true"


def _node_preview_command(package_manager: str, *, nextjs: bool) -> str:
    if package_manager == "pnpm":
        extra = "-- --hostname 0.0.0.0 --port {port}" if nextjs else "-- --host 0.0.0.0 --port {port}"
        return f"pnpm run dev {extra}"
    if package_manager == "yarn":
        extra = "--hostname 0.0.0.0 --port {port}" if nextjs else "--host 0.0.0.0 --port {port}"
        return f"yarn dev {extra}"
    extra = "-- --hostname 0.0.0.0 --port {port}" if nextjs else "-- --host 0.0.0.0 --port {port}"
    return f"npm run dev {extra}"


def _node_build_command(package_manager: str) -> str:
    if package_manager == "pnpm":
        return "pnpm run build"
    if package_manager == "yarn":
        return "yarn build"
    return "npm run build"


def _prisma_smoke_command() -> str:
    return (
        "node -e \""
        "const { PrismaClient } = require('@prisma/client');"
        "let client;"
        "try {"
        "  const { PrismaLibSql } = require('@prisma/adapter-libsql');"
        "  const adapter = new PrismaLibSql({ url: 'file:./dev.db' });"
        "  client = new PrismaClient({ adapter });"
        "} catch (error) {"
        "  client = new PrismaClient();"
        "}"
        "Promise.resolve(client.$disconnect())"
        ".then(() => process.exit(0))"
        ".catch((error) => { console.error(error); process.exit(1); });"
        "\""
    )
