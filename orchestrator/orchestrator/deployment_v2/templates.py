"""Jinja2 template renderer for deployment artifacts.

Templates are versioned via `TEMPLATES_VERSION` — bump on incompatible
template changes so `ArtifactFingerprint` reflects the rendering generation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES_VERSION = "1"

_TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "templates" / "deployment"


class TemplateRenderer:
    """Resolves and renders Jinja2 templates from the on-disk template tree."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or _TEMPLATES_ROOT
        self._env = Environment(
            loader=FileSystemLoader(str(self._root)),
            autoescape=False,
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=False,
            lstrip_blocks=False,
        )

    def render(self, template_name: str, ctx: dict[str, Any]) -> str:
        template = self._env.get_template(template_name)
        return template.render(**ctx)

    @property
    def root(self) -> Path:
        return self._root
