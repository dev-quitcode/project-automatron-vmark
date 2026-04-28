"""Constant secret names for the Kamal strategy.

Per-app application secrets (e.g. DATABASE_URL) are passed dynamically via
`DeploymentProfile.secret_env_names` and are not enumerated here.
"""

from __future__ import annotations

KAMAL_REGISTRY_PASSWORD = "KAMAL_REGISTRY_PASSWORD"
KAMAL_SSH_PRIVATE_KEY = "KAMAL_SSH_PRIVATE_KEY"

KAMAL_FIXED_SECRET_NAMES: tuple[str, ...] = (
    KAMAL_REGISTRY_PASSWORD,
    KAMAL_SSH_PRIVATE_KEY,
)
