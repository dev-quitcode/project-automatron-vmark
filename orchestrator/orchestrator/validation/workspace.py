"""Structured workspace and deploy artifact validation."""

from __future__ import annotations

import json
import shlex
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from orchestrator.validation.runtime import PreviewRuntimeSpec, resolve_preview_runtime_spec

ValidationStatus = Literal["warning", "ambiguity", "blocker"]


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    status: ValidationStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.status in {"ambiguity", "blocker"}


@dataclass
class StackValidatorResult:
    stack: str
    ok: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    runtime_spec: PreviewRuntimeSpec | None = None

    @property
    def blocking_issues(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.blocking]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stack": self.stack,
            "ok": self.ok,
            "issues": [asdict(issue) for issue in self.issues],
            "runtime_spec": asdict(self.runtime_spec) if self.runtime_spec else None,
        }


def validate_workspace_contract(
    workspace_path: Path,
    *,
    stack_config: dict[str, Any] | None = None,
    include_release_artifacts: bool = True,
) -> StackValidatorResult:
    runtime_spec = resolve_preview_runtime_spec(workspace_path, stack_config)
    issues: list[ValidationIssue] = []

    if include_release_artifacts:
        issues.extend(_validate_artifact_shapes(workspace_path))

    stack_key = _detect_stack_key(workspace_path, stack_config, runtime_spec)
    if stack_key.startswith("nextjs"):
        issues.extend(_validate_nextjs_contract(workspace_path, runtime_spec, require_prisma="prisma" in stack_key))

    return StackValidatorResult(
        stack=stack_key,
        ok=not any(issue.blocking for issue in issues),
        issues=issues,
        runtime_spec=runtime_spec,
    )


async def validate_workspace_contract_async(
    workspace_path: Path,
    *,
    stack_config: dict[str, Any] | None = None,
    container_manager: Any | None = None,
    container_id: str | None = None,
    require_heavy_checks: bool = False,
    include_release_artifacts: bool = True,
) -> StackValidatorResult:
    result = validate_workspace_contract(
        workspace_path,
        stack_config=stack_config,
        include_release_artifacts=include_release_artifacts,
    )
    if (
        require_heavy_checks
        and container_manager
        and container_id
        and result.runtime_spec is not None
    ):
        heavy_issues = await run_heavy_checks_async(
            container_manager,
            container_id,
            result.runtime_spec,
            require_prisma="prisma" in result.stack,
        )
        result.issues.extend(heavy_issues)
        result.ok = not any(issue.blocking for issue in result.issues)
    return result


def should_run_heavy_task_checks(task_text: str) -> bool:
    lowered = (task_text or "").lower()
    heavy_keywords = (
        "scaffold",
        "bootstrap",
        "install",
        "migration",
        "migrate",
        "schema",
        "prisma",
        "database",
        "docker",
        "workflow",
        "build",
    )
    return any(keyword in lowered for keyword in heavy_keywords)


def should_validate_release_artifacts(
    task_text: str,
    *,
    completed_tasks: int = 0,
    total_tasks: int = 0,
) -> bool:
    lowered = (task_text or "").lower()
    deploy_keywords = (
        "dockerfile",
        "docker",
        "deploy",
        "workflow",
        "github actions",
        "ci/cd",
        "compose",
        "release",
    )
    if any(keyword in lowered for keyword in deploy_keywords):
        return True
    if total_tasks > 0 and completed_tasks >= max(total_tasks - 3, 0):
        return True
    return False


def _validate_artifact_shapes(workspace_path: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    dockerfile = workspace_path / "Dockerfile"
    if not dockerfile.exists():
        issues.append(_blocker("artifact_dockerfile_missing", "Missing Dockerfile"))
    else:
        docker_text = _safe_read_text(dockerfile)
        if "FROM " not in docker_text.upper():
            issues.append(_blocker("artifact_dockerfile_from_missing", "Dockerfile is missing a FROM instruction"))
        if "CMD " not in docker_text.upper() and "ENTRYPOINT" not in docker_text.upper():
            issues.append(_blocker("artifact_dockerfile_entrypoint_missing", "Dockerfile is missing a runnable CMD or ENTRYPOINT"))

    env_example = workspace_path / ".env.example"
    if not env_example.exists():
        issues.append(_blocker("artifact_env_example_missing", "Missing .env.example"))
    else:
        env_lines = [
            line.strip()
            for line in _safe_read_text(env_example).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not any("=" in line for line in env_lines):
            issues.append(_blocker("artifact_env_example_invalid", ".env.example does not contain any KEY=value assignments"))

    compose_file = workspace_path / "deploy" / "docker-compose.yml"
    if not compose_file.exists():
        issues.append(_blocker("artifact_deploy_compose_missing", "Missing deploy/docker-compose.yml"))
    else:
        try:
            compose_data = yaml.safe_load(_safe_read_text(compose_file)) or {}
        except yaml.YAMLError as exc:
            issues.append(
                _blocker(
                    "artifact_deploy_compose_invalid_yaml",
                    "deploy/docker-compose.yml is not valid YAML",
                    details={"error": str(exc)},
                )
            )
        else:
            services = compose_data.get("services") if isinstance(compose_data, dict) else None
            app_service = services.get("app") if isinstance(services, dict) else None
            if not isinstance(app_service, dict):
                issues.append(_blocker("artifact_deploy_compose_missing_app", "deploy/docker-compose.yml is missing services.app"))
            elif "build" not in app_service and "image" not in app_service:
                issues.append(_blocker("artifact_deploy_compose_missing_build", "services.app must define build or image"))

    deploy_md = workspace_path / "DEPLOY.md"
    if not deploy_md.exists():
        issues.append(_blocker("artifact_deploy_md_missing", "Missing DEPLOY.md"))
    else:
        deploy_text = _safe_read_text(deploy_md)
        if "docker compose -f deploy/docker-compose.yml up -d --build" not in deploy_text:
            issues.append(_blocker("artifact_deploy_md_command_missing", "DEPLOY.md does not document the docker compose deploy command"))

    ci_workflow = workspace_path / ".github" / "workflows" / "ci.yml"
    if not ci_workflow.exists():
        issues.append(_blocker("artifact_ci_workflow_missing", "Missing .github/workflows/ci.yml"))
    else:
        ci_text = _safe_read_text(ci_workflow)
        if "feature/**" not in ci_text and "develop" not in ci_text:
            issues.append(_blocker("artifact_ci_workflow_branches_missing", "CI workflow does not trigger on feature/** or develop"))

    deploy_workflow = workspace_path / ".github" / "workflows" / "deploy.yml"
    if not deploy_workflow.exists():
        issues.append(_blocker("artifact_deploy_workflow_missing", "Missing .github/workflows/deploy.yml"))
    else:
        deploy_text = _safe_read_text(deploy_workflow)
        if "main" not in deploy_text:
            issues.append(_blocker("artifact_deploy_workflow_main_missing", "Deploy workflow does not trigger on main"))
        if "environment:" not in deploy_text:
            issues.append(_blocker("artifact_deploy_workflow_environment_missing", "Deploy workflow is missing environment configuration"))

    return issues


def _validate_nextjs_contract(
    workspace_path: Path,
    runtime_spec: PreviewRuntimeSpec,
    *,
    require_prisma: bool,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    health_route = workspace_path / "app" / "api" / "health" / "route.ts"
    if not health_route.exists():
        issues.append(_blocker("stack_next_health_missing", "Missing Next.js health endpoint at app/api/health/route.ts"))

    layout_file = workspace_path / "app" / "layout.tsx"
    if not layout_file.exists():
        issues.append(_blocker("stack_next_layout_missing", "Missing app/layout.tsx"))
    else:
        layout_text = _safe_read_text(layout_file)
        if "Create Next App" in layout_text or "Generated by create next app" in layout_text:
            issues.append(_ambiguity("stack_next_default_metadata", "Default Next.js metadata is still present in app/layout.tsx"))

    if not runtime_spec.preview_command_template:
        issues.append(_blocker("stack_next_preview_command_missing", "Preview command could not be derived deterministically for the Next.js stack"))

    if require_prisma and not (workspace_path / "prisma" / "schema.prisma").exists():
        issues.append(_blocker("stack_prisma_schema_missing", "Expected prisma/schema.prisma for the Prisma-backed Next.js stack"))

    return issues


def _render_container_command(command: str) -> str:
    """Render shell-safe commands for nested bash execution in containers.

    In particular, `node -e "..."` scripts may contain `$disconnect()` and
    similar identifiers that get mangled by shell interpolation if passed
    through multiple shell layers directly.
    """

    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return command

    if len(argv) >= 3 and argv[0] == "node" and argv[1] == "-e":
        script = argv[2]
        # Materialize the script under /workspace so Node resolves project
        # dependencies like @prisma/client using the generated app's tree
        # instead of /tmp, which breaks module resolution for smoke checks.
        script_path = f"/workspace/.automatron-validate-{uuid.uuid4().hex}.js"
        return (
            f"cat > {script_path} <<'__AUTOMATRON_NODE__'\n"
            f"{script}\n"
            "__AUTOMATRON_NODE__\n"
            f"node {script_path}; "
            "EXIT_CODE=$?; "
            f"rm -f {script_path}; "
            "exit $EXIT_CODE"
        )

    return command


async def run_heavy_checks_async(
    container_manager: Any,
    container_id: str,
    runtime_spec: PreviewRuntimeSpec,
    *,
    require_prisma: bool,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if runtime_spec.build_command:
        rendered_build_command = _render_container_command(runtime_spec.build_command)
        build_result = await container_manager.exec_in_container(
            container_id,
            f"cd /workspace && {rendered_build_command}",
            timeout=360,
        )
        if build_result.exit_code != 0:
            issues.append(
                _blocker(
                    "heavy_build_failed",
                    "Workspace build command failed",
                    details={"output": build_result.output[-2000:]},
                )
            )

    if require_prisma and runtime_spec.prisma_smoke_command:
        rendered_prisma_command = _render_container_command(runtime_spec.prisma_smoke_command)
        prisma_result = await container_manager.exec_in_container(
            container_id,
            f"cd /workspace && {rendered_prisma_command}",
            timeout=120,
        )
        if prisma_result.exit_code != 0:
            issues.append(
                _blocker(
                    "heavy_prisma_import_failed",
                    "Prisma client import smoke test failed",
                    details={"output": prisma_result.output[-2000:]},
                )
            )

    return issues


def _detect_stack_key(
    workspace_path: Path,
    stack_config: dict[str, Any] | None,
    runtime_spec: PreviewRuntimeSpec,
) -> str:
    stack_text = json.dumps(stack_config or {}, ensure_ascii=True).lower()
    if runtime_spec.stack == "nextjs":
        if "prisma" in stack_text or (workspace_path / "prisma" / "schema.prisma").exists():
            return "nextjs-prisma-sqlite-tailwind"
        return "nextjs"
    return runtime_spec.stack


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _blocker(code: str, message: str, *, details: dict[str, Any] | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, status="blocker", message=message, details=details or {})


def _ambiguity(code: str, message: str, *, details: dict[str, Any] | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, status="ambiguity", message=message, details=details or {})
