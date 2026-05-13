"""Structured preflight checks for project start and deploy operations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import httpx

from orchestrator.config import settings
from orchestrator.github.issues import GitHubClient
from orchestrator.llm.catalog import get_provider_model_catalog
from orchestrator.llm.configuration import default_llm_config, normalize_llm_config, provider_api_key
from orchestrator.repository.manager import RepositoryManager

PreflightPhase = Literal["start", "deploy"]
DeploymentPreflightPhase = Literal[
    "generate_artifacts", "setup", "deploy", "health_verify"
]
PreflightStatus = Literal["ok", "warning", "blocking"]


@dataclass(frozen=True)
class PreflightCheck:
    code: str
    status: PreflightStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreflightResult:
    phase: PreflightPhase
    ok: bool
    blocking: bool
    checks: list[PreflightCheck] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "ok": self.ok,
            "blocking": self.blocking,
            "checks": [asdict(check) for check in self.checks],
        }


class PreflightService:
    """Validates local runtime prerequisites and remote deploy prerequisites."""

    def __init__(
        self,
        *,
        repository_manager: RepositoryManager | None = None,
    ) -> None:
        self.repository_manager = repository_manager or RepositoryManager()

    async def run(self, phase: PreflightPhase, *, project: dict[str, Any]) -> PreflightResult:
        if phase == "deploy":
            checks = await self._deploy_checks(project)
        else:
            checks = await self._start_checks(project)
        blocking = any(check.status == "blocking" for check in checks)
        return PreflightResult(phase=phase, ok=not blocking, blocking=blocking, checks=checks)

    async def run_v2(
        self,
        project: dict[str, Any],
        phase: DeploymentPreflightPhase,
    ) -> PreflightResult:
        """Run strategy-aware preflight for the deployment_v2 lifecycle."""
        from orchestrator.deployment_v2 import get_strategy
        from orchestrator.deployment_v2.profile import DeploymentProfile

        strategy_name = (project.get("deployment_strategy") or "").strip()
        checks: list[PreflightCheck] = []
        checks.extend(self._github_configuration_checks())
        if phase in {"setup", "deploy"}:
            checks.append(await self._deploy_audit_gate_check(project))

        if not strategy_name:
            checks.append(
                _blocking(
                    "deployment_strategy_not_set",
                    "Project has no deployment strategy configured",
                )
            )
        else:
            try:
                strategy = get_strategy(strategy_name)
            except KeyError:
                checks.append(
                    _blocking(
                        "deployment_strategy_unknown",
                        f"Unknown deployment strategy: {strategy_name!r}",
                        details={"strategy": strategy_name},
                    )
                )
            else:
                profile_data = project.get("deployment_profile") or {}
                if profile_data:
                    profile = DeploymentProfile.from_dict(profile_data)
                else:
                    profile = strategy.detect_requirements(project)
                checks.extend(await strategy.preflight(profile, phase, project=project))

        blocking = any(check.status == "blocking" for check in checks)
        return PreflightResult(
            phase="deploy",  # report under the existing Literal for compatibility
            ok=not blocking,
            blocking=blocking,
            checks=checks,
        )

    async def _deploy_audit_gate_check(self, project: dict[str, Any]) -> PreflightCheck:
        owner = (project.get("github_repo_owner") or "").strip()
        repo = (project.get("github_repo_name") or "").strip()
        issue_number = project.get("deploy_audit_issue_number")
        issue_url = project.get("deploy_audit_issue_url")

        if not owner or not repo:
            return _blocking(
                "deploy_audit_issue_missing",
                "Deploy audit issue gate cannot be evaluated: repository is not configured",
                details={"state": "missing"},
            )
        if not issue_number:
            return _blocking(
                "deploy_audit_issue_missing",
                "Deploy audit issue gate is missing. Generate deploy artifacts first.",
                details={"state": "missing", "issue_number": None, "issue_url": None},
            )

        gh = GitHubClient()
        try:
            issue = await gh.get_issue(owner, repo, int(issue_number))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return _blocking(
                    "deploy_audit_issue_missing",
                    "Deploy audit issue was not found on GitHub. Regenerate deploy artifacts.",
                    details={
                        "state": "missing",
                        "issue_number": int(issue_number),
                        "issue_url": issue_url,
                    },
                )
            return _blocking(
                "deploy_audit_issue_unknown",
                "Failed to read deploy audit issue state from GitHub",
                details={
                    "issue_number": int(issue_number),
                    "issue_url": issue_url,
                    "error": str(exc),
                },
            )

        state = (issue.get("state") or "").lower()
        if state == "closed":
            return _ok(
                "deploy_audit_gate",
                "Deploy audit issue is closed; setup/deploy gate is satisfied",
                details={
                    "state": state,
                    "issue_number": int(issue.get("number") or issue_number),
                    "issue_url": issue.get("html_url") or issue_url,
                },
            )
        return _blocking(
            "deploy_audit_issue_open",
            "Deploy audit issue is still open. Close it after readiness verification before setup/deploy.",
            details={
                "state": state or "open",
                "issue_number": int(issue.get("number") or issue_number),
                "issue_url": issue.get("html_url") or issue_url,
            },
        )

    def normalize_deploy_target(self, deploy_target: dict[str, Any] | None) -> dict[str, Any]:
        target = dict(deploy_target or {})
        target["auth_mode"] = str(target.get("auth_mode") or "ssh_key").strip().lower()
        target["host"] = str(target.get("host") or "").strip()
        target["port"] = int(target.get("port") or 22)
        target["user"] = str(target.get("user") or "").strip()
        target["deploy_path"] = str(target.get("deploy_path") or "").strip()
        target["auth_reference"] = str(target.get("auth_reference") or "").strip() or None
        target["ssh_private_key"] = str(target.get("ssh_private_key") or "").strip() or None
        target["ssh_password"] = str(target.get("ssh_password") or "").strip() or None
        target["known_hosts"] = str(target.get("known_hosts") or "").strip() or None
        target["env_content"] = str(target.get("env_content") or "").strip() or None
        target["app_url"] = str(target.get("app_url") or "").strip() or None
        target["health_path"] = _normalize_health_path(str(target.get("health_path") or "").strip())
        return target

    def validate_deploy_target_shape(self, deploy_target: dict[str, Any] | None) -> list[PreflightCheck]:
        target = self.normalize_deploy_target(deploy_target)
        checks: list[PreflightCheck] = []

        required = ("host", "user", "deploy_path")
        missing = [field for field in required if not target.get(field)]
        if missing:
            checks.append(
                _blocking(
                    "deploy_target_missing_fields",
                    f"Deploy target is missing required fields: {', '.join(missing)}",
                    details={"missing": missing},
                )
            )
        else:
            checks.append(_ok("deploy_target_shape", "Deploy target includes required host/user/path fields"))

        if target["auth_mode"] not in {"ssh_key", "password"}:
            checks.append(
                _blocking(
                    "deploy_target_auth_mode_invalid",
                    f"Unsupported deploy auth mode: {target['auth_mode']}",
                    details={"auth_mode": target["auth_mode"]},
                )
            )
        elif target["auth_mode"] == "ssh_key" and not target.get("ssh_private_key") and not target.get("auth_reference"):
            checks.append(
                _blocking(
                    "deploy_target_ssh_key_missing",
                    "SSH key deploy mode requires ssh_private_key or auth_reference",
                )
            )
        elif target["auth_mode"] == "password" and not target.get("ssh_password") and not target.get("auth_reference"):
            checks.append(
                _blocking(
                    "deploy_target_password_missing",
                    "Password deploy mode requires ssh_password or auth_reference",
                )
            )
        else:
            checks.append(_ok("deploy_target_auth_mode", f"Deploy auth mode `{target['auth_mode']}` is configured"))

        checks.append(
            _ok(
                "deploy_target_health_path",
                f"Health path normalized to {target['health_path']}",
                details={"health_path": target["health_path"]},
            )
        )
        return checks

    async def _start_checks(self, project: dict[str, Any]) -> list[PreflightCheck]:
        checks: list[PreflightCheck] = []
        checks.extend(self._github_configuration_checks())
        checks.extend(await self._llm_provider_checks(project))
        return checks

    async def _deploy_checks(self, project: dict[str, Any]) -> list[PreflightCheck]:
        checks: list[PreflightCheck] = []
        checks.extend(self._github_configuration_checks())
        checks.extend(self.validate_deploy_target_shape(project.get("deploy_target")))

        repo_name = str(project.get("repo_name") or "").strip()
        if not repo_name:
            checks.append(_blocking("deploy_repo_missing", "Project does not have a configured repository"))
            return checks

        repo = await self.repository_manager.get_remote_repository(repo_name)
        if repo is None:
            checks.append(
                _blocking(
                    "deploy_repo_access_failed",
                    "GitHub repository could not be found or accessed",
                    details={"repo_name": repo_name},
                )
            )
            return checks
        checks.append(
            _ok(
                "deploy_repo_access",
                "GitHub repository is accessible",
                details={"repo_name": repo_name, "repo_url": repo.get("html_url")},
            )
        )

        environment_name = project.get("github_environment_name") or settings.github_environment_name
        try:
            await self.repository_manager.actions_manager.ensure_environment(
                repo_name,
                environment_name=environment_name,
            )
            public_key = await self.repository_manager.actions_manager.get_environment_public_key(
                repo_name,
                environment_name=environment_name,
            )
        except Exception as exc:
            checks.append(
                _blocking(
                    "deploy_environment_access_failed",
                    "GitHub environment creation or public key access failed",
                    details={"environment": environment_name, "error": str(exc)},
                )
            )
        else:
            checks.append(
                _ok(
                    "deploy_environment_access",
                    "GitHub environment and public key are available",
                    details={"environment": environment_name, "key_id": public_key["key_id"]},
                )
            )
        return checks

    def _github_configuration_checks(self) -> list[PreflightCheck]:
        checks: list[PreflightCheck] = []
        missing = []
        if not settings.github_token:
            missing.append("GITHUB_TOKEN")
        if not settings.github_owner:
            missing.append("GITHUB_OWNER")
        if missing:
            checks.append(
                _blocking(
                    "github_configuration_missing",
                    f"Missing required GitHub configuration: {', '.join(missing)}",
                    details={"missing": missing},
                )
            )
        else:
            checks.append(
                _ok(
                    "github_configuration_present",
                    "GitHub token and owner are configured",
                    details={"owner": settings.github_owner, "owner_type": settings.github_owner_type},
                )
            )
        return checks

    async def _llm_provider_checks(self, project: dict[str, Any]) -> list[PreflightCheck]:
        checks: list[PreflightCheck] = []
        llm_config = normalize_llm_config(project.get("llm_config") or default_llm_config())
        for role, role_config in llm_config.items():
            provider = role_config["provider"]
            if provider_api_key(provider):
                checks.append(_ok(f"llm_provider_{role}", f"{role.capitalize()} provider `{provider}` is configured"))
                catalog = await get_provider_model_catalog(provider)
                available_model_ids = {model["id"] for model in catalog.get("models", [])}
                selected_model = role_config["model"]
                if catalog.get("configured") and available_model_ids and selected_model not in available_model_ids:
                    checks.append(
                        _blocking(
                            f"llm_model_{role}_unavailable",
                            f"{role.capitalize()} model `{selected_model}` is not available for provider `{provider}`",
                            details={
                                "role": role,
                                "provider": provider,
                                "model": selected_model,
                                "available_models_preview": sorted(list(available_model_ids))[:10],
                            },
                        )
                    )
                else:
                    checks.append(
                        _ok(
                            f"llm_model_{role}",
                            f"{role.capitalize()} model `{selected_model}` is available",
                            details={"role": role, "provider": provider, "model": selected_model},
                        )
                    )
            else:
                checks.append(
                    _blocking(
                        f"llm_provider_{role}_missing_key",
                        f"{role.capitalize()} provider `{provider}` is missing an API key",
                        details={"role": role, "provider": provider},
                    )
                )
        return checks


def _normalize_health_path(value: str) -> str:
    if not value:
        return "/api/health"
    return value if value.startswith("/") else f"/{value}"


def _ok(code: str, message: str, *, details: dict[str, Any] | None = None) -> PreflightCheck:
    return PreflightCheck(code=code, status="ok", message=message, details=details or {})


def _warning(code: str, message: str, *, details: dict[str, Any] | None = None) -> PreflightCheck:
    return PreflightCheck(code=code, status="warning", message=message, details=details or {})


def _blocking(code: str, message: str, *, details: dict[str, Any] | None = None) -> PreflightCheck:
    return PreflightCheck(code=code, status="blocking", message=message, details=details or {})
