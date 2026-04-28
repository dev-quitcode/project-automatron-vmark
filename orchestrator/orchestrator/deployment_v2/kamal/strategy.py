"""KamalDeploymentStrategy — primary VPS deployment strategy."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from orchestrator.deployment_v2.kamal.config import KamalConfig
from orchestrator.deployment_v2.kamal.probes import (
    dns_resolves,
    ghcr_auth_probe,
    runtime_health_probe,
    tcp_reachable,
)
from orchestrator.deployment_v2.kamal.secrets import (
    KAMAL_REGISTRY_PASSWORD,
    KAMAL_SSH_PRIVATE_KEY,
)
from orchestrator.deployment_v2.profile import DeploymentProfile
from orchestrator.deployment_v2.stack_detector import (
    HEALTH_ROUTE_CANDIDATES,
    PROBE_FILES,
    StackDetector,
    is_supported,
)
from orchestrator.deployment_v2.strategy import (
    DeploymentStrategy,
    DispatchAction,
    PreflightPhase,
)
from orchestrator.deployment_v2.templates import TEMPLATES_VERSION, TemplateRenderer
from orchestrator.validation.preflight import PreflightCheck

logger = logging.getLogger(__name__)


def _ok(code: str, message: str, **details: Any) -> PreflightCheck:
    return PreflightCheck(code=code, status="ok", message=message, details=details)


def _warning(code: str, message: str, **details: Any) -> PreflightCheck:
    return PreflightCheck(code=code, status="warning", message=message, details=details)


def _blocking(code: str, message: str, **details: Any) -> PreflightCheck:
    return PreflightCheck(code=code, status="blocking", message=message, details=details)


KAMAL_DEFAULT_VERSION = "2.4.0"


class KamalDeploymentStrategy(DeploymentStrategy):
    name = "kamal"
    version = "1"
    rollback_metadata_required = True

    def __init__(
        self,
        *,
        renderer: TemplateRenderer | None = None,
        kamal_version: str = KAMAL_DEFAULT_VERSION,
    ) -> None:
        self._renderer = renderer or TemplateRenderer()
        self._kamal_version = kamal_version
        self._detector = StackDetector()

    def config_schema(self) -> type[BaseModel]:
        return KamalConfig

    def secret_schema(self) -> dict[str, str]:
        return {
            KAMAL_REGISTRY_PASSWORD: "Container registry credential (e.g. GHCR PAT with write:packages)",
            KAMAL_SSH_PRIVATE_KEY: "SSH private key Kamal uses to reach the VPS",
        }

    def validate_config(self, config: dict[str, Any]) -> list[PreflightCheck]:
        try:
            KamalConfig.model_validate(config)
        except ValidationError as exc:
            return [
                _blocking(
                    "kamal_config_invalid",
                    f"Kamal config failed validation: {exc.errors()[0]['msg']}",
                    errors=exc.errors(),
                )
            ]
        return [_ok("kamal_config_valid", "Kamal config passed schema validation")]

    def normalize_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return KamalConfig.model_validate(config).model_dump()

    def detect_requirements(
        self,
        project: dict[str, Any],
        repo_files: dict[str, str] | None = None,
    ) -> DeploymentProfile:
        detection = self._detector.detect(repo_files or {})

        repo_owner = (
            project.get("github_repo_owner")
            or project.get("repo_owner")
            or ""
        )
        repo_name = (
            project.get("github_repo_name")
            or project.get("repo_name")
            or ""
        )
        service_name = (repo_name or "app").lower().replace(" ", "-")
        image = ""
        if repo_owner and repo_name:
            image = f"ghcr.io/{repo_owner.lower()}/{repo_name.lower()}"

        existing = project.get("deployment_profile") or {}
        profile = DeploymentProfile(
            strategy=self.name,
            framework=detection.framework,
            package_manager=detection.package_manager,
            router_style=detection.router_style,
            src_layout=detection.src_layout,
            next_output=detection.next_output,
            health_route_files=detection.health_route_files,
            host=existing.get("host", ""),
            ssh_user=existing.get("ssh_user", ""),
            ssh_port=int(existing.get("ssh_port") or 22),
            domain=existing.get("domain", ""),
            container_port=int(existing.get("container_port") or 3000),
            health_path=existing.get("health_path") or "/api/health",
            registry=existing.get("registry") or "ghcr.io",
            registry_username=existing.get("registry_username", ""),
            image=existing.get("image") or image,
            clear_env=dict(existing.get("clear_env") or {}),
            secret_env_names=list(existing.get("secret_env_names") or []),
            auto_deploy_on_main=bool(existing.get("auto_deploy_on_main") or False),
            artifacts_push_mode=existing.get("artifacts_push_mode") or "pr",
            kamal_version=self._kamal_version,
            environment_name=project.get("github_environment_name") or "production",
            service_name=service_name,
        )
        return profile

    def merge_config_into_profile(
        self,
        profile: DeploymentProfile,
        config: dict[str, Any],
    ) -> DeploymentProfile:
        normalized = self.normalize_config(config)
        profile.host = normalized["host"]
        profile.ssh_user = normalized["ssh_user"]
        profile.ssh_port = int(normalized["ssh_port"])
        profile.domain = normalized["domain"]
        profile.container_port = int(normalized["container_port"])
        profile.health_path = normalized["health_path"]
        profile.registry = normalized["registry"]
        profile.registry_username = normalized["registry_username"]
        if normalized.get("image"):
            profile.image = normalized["image"]
        profile.clear_env = dict(normalized.get("clear_env") or {})
        profile.secret_env_names = list(normalized.get("secret_env_names") or [])
        profile.auto_deploy_on_main = bool(normalized.get("auto_deploy_on_main") or False)
        profile.artifacts_push_mode = normalized.get("artifacts_push_mode") or "pr"
        return profile

    def render_artifacts(self, profile: DeploymentProfile) -> dict[str, str]:
        ctx = self._template_context(profile)
        files: dict[str, str] = {
            "Dockerfile": self._renderer.render("docker/nextjs.Dockerfile.j2", ctx),
            ".dockerignore": self._renderer.render("docker/dockerignore.j2", ctx),
            "config/deploy.yml": self._renderer.render("kamal/deploy.yml.j2", ctx),
            ".kamal/secrets.example": self._renderer.render("kamal/secrets.example.j2", ctx),
            "DEPLOYMENT.md": self._renderer.render("kamal/DEPLOYMENT.md.j2", ctx),
        }
        return files

    def workflow_files(self, profile: DeploymentProfile) -> dict[str, str]:
        ctx = self._template_context(profile)
        return {
            ".github/workflows/ci.yml": self._renderer.render("ci/node-ci.yml.j2", ctx),
            ".github/workflows/deploy.yml": self._renderer.render(
                "workflows/kamal-deploy.yml.j2", ctx
            ),
        }

    def dispatch_inputs(
        self,
        profile: DeploymentProfile,
        action: DispatchAction,
        automatron_run_id: str,
        rollback_to: str | None = None,
    ) -> dict[str, str]:
        if not automatron_run_id:
            raise ValueError("automatron_run_id is required for workflow dispatch")
        inputs = {"action": action, "automatron_run_id": automatron_run_id}
        if action == "rollback":
            if not rollback_to:
                raise ValueError("rollback_to is required for rollback action")
            inputs["rollback_to"] = rollback_to
        elif rollback_to:
            inputs["rollback_to"] = rollback_to
        return inputs

    async def preflight(
        self,
        profile: DeploymentProfile,
        phase: PreflightPhase,
        *,
        project: dict[str, Any] | None = None,
    ) -> list[PreflightCheck]:
        checks: list[PreflightCheck] = []
        checks.extend(self._stack_checks(profile))

        if phase in {"setup", "deploy"}:
            checks.extend(await self._network_checks(profile, blocking_dns=True, blocking_ssh=True))
            checks.extend(await self._registry_checks(profile))
        elif phase == "generate_artifacts":
            checks.extend(await self._network_checks(profile, blocking_dns=False, blocking_ssh=False))
        elif phase == "health_verify":
            checks.extend(await self._health_checks(profile, project or {}))

        if phase == "deploy":
            checks.extend(await self._health_checks(profile, project or {}))
        return checks

    def template_probe_files(self) -> tuple[str, ...]:
        return PROBE_FILES

    def health_route_candidates(self) -> tuple[str, ...]:
        return HEALTH_ROUTE_CANDIDATES

    def _template_context(self, profile: DeploymentProfile) -> dict[str, Any]:
        return {
            **profile.to_dict(),
            "templates_version": TEMPLATES_VERSION,
            "kamal_version": profile.kamal_version or self._kamal_version,
        }

    def _stack_checks(self, profile: DeploymentProfile) -> list[PreflightCheck]:
        checks: list[PreflightCheck] = []
        if profile.framework != "nextjs":
            checks.append(
                _blocking(
                    "stack_unsupported_framework",
                    f"Detected framework {profile.framework!r} is not supported in Slice 1 (Next.js only)",
                    framework=profile.framework,
                )
            )
        else:
            checks.append(
                _ok("stack_framework_nextjs", "Detected Next.js framework", framework=profile.framework)
            )

        if profile.package_manager != "npm":
            checks.append(
                _blocking(
                    "stack_unsupported_package_manager",
                    f"Detected package manager {profile.package_manager!r} is not supported in Slice 1 (npm only)",
                    package_manager=profile.package_manager,
                )
            )
        else:
            checks.append(_ok("stack_package_manager_npm", "Detected npm package manager"))

        if profile.next_output == "standalone":
            checks.append(
                _ok(
                    "stack_next_output_standalone",
                    "next.config has output: 'standalone' — using slim runtime image",
                )
            )
        elif profile.next_output == "default":
            checks.append(
                _warning(
                    "stack_next_output_default",
                    "Next.js is not configured for output: 'standalone'; recommend enabling for smaller production image",
                )
            )
        else:
            checks.append(
                _warning(
                    "stack_next_output_unknown",
                    "Could not determine next.config output mode; falling back to default Dockerfile",
                )
            )

        return checks

    async def _network_checks(
        self,
        profile: DeploymentProfile,
        *,
        blocking_dns: bool,
        blocking_ssh: bool,
    ) -> list[PreflightCheck]:
        checks: list[PreflightCheck] = []

        dns_ok, addrs = await dns_resolves(profile.domain)
        if not dns_ok:
            check = _blocking if blocking_dns else _warning
            checks.append(
                check(
                    "preflight_dns_unresolved",
                    f"Domain {profile.domain!r} did not resolve to any A/AAAA records",
                )
            )
        elif profile.host and profile.host not in addrs:
            check = _blocking if blocking_dns else _warning
            checks.append(
                check(
                    "preflight_dns_mismatch",
                    f"Domain {profile.domain!r} resolves to {addrs} but configured host is {profile.host!r}",
                    resolved=addrs,
                    host=profile.host,
                )
            )
        else:
            checks.append(
                _ok(
                    "preflight_dns_ok",
                    f"Domain {profile.domain!r} resolves to {addrs}",
                    resolved=addrs,
                )
            )

        ssh_ok = await tcp_reachable(profile.host, profile.ssh_port, timeout=5.0)
        if ssh_ok:
            checks.append(
                _ok(
                    "preflight_ssh_reachable",
                    f"TCP {profile.host}:{profile.ssh_port} reachable",
                )
            )
        else:
            check = _blocking if blocking_ssh else _warning
            checks.append(
                check(
                    "preflight_ssh_unreachable",
                    f"TCP {profile.host}:{profile.ssh_port} not reachable",
                )
            )
        return checks

    async def _registry_checks(self, profile: DeploymentProfile) -> list[PreflightCheck]:
        # We don't have the password here — strategy never sees secret values.
        # Instead, the API layer runs ghcr_auth_probe right after PUT /deploy-target
        # and surfaces the result via a separate preflight phase. This method only
        # checks that the registry config shape looks plausible.
        if profile.registry != "ghcr.io":
            return [
                _warning(
                    "preflight_registry_non_ghcr",
                    f"Registry {profile.registry!r} is not GHCR; auth probe must be run separately",
                )
            ]
        if not profile.registry_username:
            return [
                _blocking(
                    "preflight_registry_username_missing",
                    "Registry username is required",
                )
            ]
        return [
            _ok(
                "preflight_registry_shape_ok",
                "Registry config shape valid; live auth probe is run separately",
            )
        ]

    async def _health_checks(
        self,
        profile: DeploymentProfile,
        project: dict[str, Any],
    ) -> list[PreflightCheck]:
        checks: list[PreflightCheck] = []

        if profile.health_route_files:
            checks.append(
                _ok(
                    "preflight_health_route_present",
                    f"Health route files detected: {profile.health_route_files[0]}",
                    files=profile.health_route_files,
                )
            )
        else:
            checks.append(
                _warning(
                    "preflight_health_route_not_found",
                    f"No health route file matched candidates {list(self.health_route_candidates())}",
                    candidates=list(self.health_route_candidates()),
                )
            )

        preview_url = (project.get("preview_url") or "").strip()
        if preview_url:
            ok, status = await runtime_health_probe(preview_url, profile.health_path)
            if ok:
                checks.append(
                    _ok(
                        "preflight_runtime_health_ok",
                        f"Runtime probe {preview_url}{profile.health_path} returned {status}",
                        status=status,
                    )
                )
            else:
                checks.append(
                    _blocking(
                        "preflight_runtime_health_failed",
                        f"Runtime probe {preview_url}{profile.health_path} returned {status}",
                        status=status,
                    )
                )
        else:
            checks.append(
                _warning(
                    "preflight_runtime_health_skipped",
                    "No preview_url available; runtime health probe skipped",
                )
            )
        return checks
