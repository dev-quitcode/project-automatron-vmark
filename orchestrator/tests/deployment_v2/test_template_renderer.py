"""Tests for TemplateRenderer."""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import UndefinedError

from orchestrator.deployment_v2.templates import TEMPLATES_VERSION, TemplateRenderer


def test_renderer_uses_strict_undefined(tmp_path: Path):
    template_dir = tmp_path
    (template_dir / "fail.j2").write_text("hello {{ missing_variable }}")
    renderer = TemplateRenderer(root=template_dir)
    with pytest.raises(UndefinedError):
        renderer.render("fail.j2", {})


def test_renderer_renders_known_variables(tmp_path: Path):
    template_dir = tmp_path
    (template_dir / "ok.j2").write_text("hello {{ name }}")
    renderer = TemplateRenderer(root=template_dir)
    assert renderer.render("ok.j2", {"name": "world"}).strip() == "hello world"


def test_templates_version_is_string():
    assert isinstance(TEMPLATES_VERSION, str)
    assert TEMPLATES_VERSION  # non-empty
