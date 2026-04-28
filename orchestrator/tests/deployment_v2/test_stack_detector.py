"""Tests for the deterministic stack detector."""

from __future__ import annotations

import json

from orchestrator.deployment_v2.stack_detector import StackDetector, is_supported


def _pkg(deps: dict[str, str], dev_deps: dict[str, str] | None = None) -> str:
    return json.dumps(
        {
            "name": "test",
            "version": "0.0.1",
            "dependencies": deps,
            **({"devDependencies": dev_deps} if dev_deps else {}),
        }
    )


def test_detects_nextjs_with_npm():
    files = {
        "package.json": _pkg({"next": "^15.0.0"}),
        "package-lock.json": "{}",
        "next.config.mjs": "export default { output: 'standalone' };",
        "app/api/health/route.ts": "export const GET = () => new Response('ok');",
    }
    result = StackDetector().detect(files)
    assert result.framework == "nextjs"
    assert result.package_manager == "npm"
    assert result.next_output == "standalone"
    assert result.router_style == "app"
    assert "app/api/health/route.ts" in result.health_route_files
    assert is_supported(result)


def test_unknown_framework_when_next_missing():
    files = {"package.json": _pkg({"react": "^18.0.0"})}
    result = StackDetector().detect(files)
    assert result.framework == "unsupported"


def test_pnpm_lockfile_returns_pnpm_package_manager():
    files = {
        "package.json": _pkg({"next": "^15.0.0"}),
        "pnpm-lock.yaml": "lockfileVersion: 9",
    }
    result = StackDetector().detect(files)
    assert result.package_manager == "pnpm"
    assert not is_supported(result)


def test_yarn_lockfile_returns_yarn():
    files = {
        "package.json": _pkg({"next": "^15.0.0"}),
        "yarn.lock": "# yarn lockfile",
    }
    result = StackDetector().detect(files)
    assert result.package_manager == "yarn"
    assert not is_supported(result)


def test_bun_lockfile_returns_bun():
    files = {
        "package.json": _pkg({"next": "^15.0.0"}),
        "bun.lockb": "binary",
    }
    result = StackDetector().detect(files)
    assert result.package_manager == "bun"
    assert not is_supported(result)


def test_package_json_without_lockfile_defaults_to_npm():
    files = {"package.json": _pkg({"next": "^15.0.0"})}
    result = StackDetector().detect(files)
    assert result.package_manager == "npm"


def test_pages_router_detection():
    files = {
        "package.json": _pkg({"next": "^15.0.0"}),
        "package-lock.json": "{}",
        "pages/api/health.ts": "export default () => {};",
        "pages/index.tsx": "export default function Page() { return null; }",
    }
    result = StackDetector().detect(files)
    assert result.router_style == "pages"
    assert "pages/api/health.ts" in result.health_route_files


def test_src_layout_with_app_router():
    files = {
        "package.json": _pkg({"next": "^15.0.0"}),
        "package-lock.json": "{}",
        "src/app/layout.tsx": "export default () => null;",
        "src/app/api/health/route.ts": "export const GET = () => new Response('ok');",
    }
    result = StackDetector().detect(files)
    assert result.router_style == "app"
    assert result.src_layout is True


def test_default_next_output_when_no_standalone():
    files = {
        "package.json": _pkg({"next": "^15.0.0"}),
        "package-lock.json": "{}",
        "next.config.js": "module.exports = { reactStrictMode: true };",
    }
    result = StackDetector().detect(files)
    assert result.next_output == "default"


def test_unknown_next_output_when_no_config_file():
    files = {
        "package.json": _pkg({"next": "^15.0.0"}),
        "package-lock.json": "{}",
    }
    result = StackDetector().detect(files)
    assert result.next_output == "unknown"
