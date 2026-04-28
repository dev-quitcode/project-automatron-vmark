"""StrategyRegistry — single lookup point for deployment strategies."""

from __future__ import annotations

from orchestrator.deployment_v2.strategy import DeploymentStrategy


class StrategyRegistry:
    """In-process registry of available deployment strategies."""

    def __init__(self) -> None:
        self._strategies: dict[str, DeploymentStrategy] = {}

    def register(self, strategy: DeploymentStrategy) -> None:
        if not strategy.name:
            raise ValueError("DeploymentStrategy.name must be non-empty")
        self._strategies[strategy.name] = strategy

    def get(self, name: str) -> DeploymentStrategy:
        if name not in self._strategies:
            raise KeyError(f"Unknown deployment strategy: {name!r}")
        return self._strategies[name]

    def has(self, name: str) -> bool:
        return name in self._strategies

    def names(self) -> list[str]:
        return sorted(self._strategies)


_REGISTRY: StrategyRegistry | None = None


def _build_default_registry() -> StrategyRegistry:
    from orchestrator.deployment_v2.kamal.strategy import KamalDeploymentStrategy

    registry = StrategyRegistry()
    registry.register(KamalDeploymentStrategy())
    return registry


def get_strategy(name: str) -> DeploymentStrategy:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_default_registry()
    return _REGISTRY.get(name)


def registry() -> StrategyRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_default_registry()
    return _REGISTRY
