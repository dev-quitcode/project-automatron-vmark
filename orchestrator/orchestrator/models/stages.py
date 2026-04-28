"""Project stage constants — Python mirror of the TypeScript ProjectStage enum.

Single source of truth for valid stage values. Keep in sync with
[web-ui/src/lib/types.ts](web-ui/src/lib/types.ts).
"""

from __future__ import annotations

from typing import Literal

ProjectStage = Literal[
    "intake",
    "planning",
    "awaiting_plan_approval",
    "repo_preparing",
    "scaffolding",
    "building",
    "awaiting_preview_approval",
    "deployment_planning",
    "deploy_target_configured",
    "deployment_artifacts_generated",
    "deployment_preflight_passed",
    "deployment_preflight_failed",
    "ready_for_deploy",
    "deploying",
    "deployed",
    "deploy_failed",
    "rolling_back",
    "rolled_back",
    "frozen",
    "error",
]

INTAKE: ProjectStage = "intake"
PLANNING: ProjectStage = "planning"
AWAITING_PLAN_APPROVAL: ProjectStage = "awaiting_plan_approval"
REPO_PREPARING: ProjectStage = "repo_preparing"
SCAFFOLDING: ProjectStage = "scaffolding"
BUILDING: ProjectStage = "building"
AWAITING_PREVIEW_APPROVAL: ProjectStage = "awaiting_preview_approval"
DEPLOYMENT_PLANNING: ProjectStage = "deployment_planning"
DEPLOY_TARGET_CONFIGURED: ProjectStage = "deploy_target_configured"
DEPLOYMENT_ARTIFACTS_GENERATED: ProjectStage = "deployment_artifacts_generated"
DEPLOYMENT_PREFLIGHT_PASSED: ProjectStage = "deployment_preflight_passed"
DEPLOYMENT_PREFLIGHT_FAILED: ProjectStage = "deployment_preflight_failed"
READY_FOR_DEPLOY: ProjectStage = "ready_for_deploy"
DEPLOYING: ProjectStage = "deploying"
DEPLOYED: ProjectStage = "deployed"
DEPLOY_FAILED: ProjectStage = "deploy_failed"
ROLLING_BACK: ProjectStage = "rolling_back"
ROLLED_BACK: ProjectStage = "rolled_back"
FROZEN: ProjectStage = "frozen"
ERROR: ProjectStage = "error"

ALL_STAGES: tuple[ProjectStage, ...] = (
    INTAKE,
    PLANNING,
    AWAITING_PLAN_APPROVAL,
    REPO_PREPARING,
    SCAFFOLDING,
    BUILDING,
    AWAITING_PREVIEW_APPROVAL,
    DEPLOYMENT_PLANNING,
    DEPLOY_TARGET_CONFIGURED,
    DEPLOYMENT_ARTIFACTS_GENERATED,
    DEPLOYMENT_PREFLIGHT_PASSED,
    DEPLOYMENT_PREFLIGHT_FAILED,
    READY_FOR_DEPLOY,
    DEPLOYING,
    DEPLOYED,
    DEPLOY_FAILED,
    ROLLING_BACK,
    ROLLED_BACK,
    FROZEN,
    ERROR,
)

DEPLOYMENT_STAGES: frozenset[ProjectStage] = frozenset(
    {
        DEPLOYMENT_PLANNING,
        DEPLOY_TARGET_CONFIGURED,
        DEPLOYMENT_ARTIFACTS_GENERATED,
        DEPLOYMENT_PREFLIGHT_PASSED,
        DEPLOYMENT_PREFLIGHT_FAILED,
        READY_FOR_DEPLOY,
        DEPLOYING,
        DEPLOYED,
        DEPLOY_FAILED,
        ROLLING_BACK,
        ROLLED_BACK,
    }
)

TERMINAL_STAGES: frozenset[ProjectStage] = frozenset({DEPLOYED, FROZEN, ERROR})
