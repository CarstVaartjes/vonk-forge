"""The resolver writes the selected services and fails loudly on a bad contract."""

from __future__ import annotations

import pytest
import yaml
from vonk_nas_setup.compose import resolve_compose
from vonk_nas_setup.filesystem import SetupError

BASE = """
services:
  core: {image: core}
  optional:
    image: optional
    profiles: [extra]
  watcher:
    image: watcher
    profiles: [other]
    depends_on: {optional: {condition: service_started, required: false}}
"""


def test_optional_dependency_on_an_unselected_service_is_dropped() -> None:
    """Catch refusing a Compose-valid `required: false` dependency."""
    written = yaml.safe_load(
        resolve_compose(BASE, {"COMPOSE_PROFILES": "other"}).decode()
    )
    assert set(written["services"]) == {"core", "watcher"}
    assert "depends_on" not in written["services"]["watcher"]


def test_required_dependency_on_an_unselected_service_fails() -> None:
    """Catch writing a Compose file that cannot start."""
    compose = BASE.replace("required: false", "required: true")
    with pytest.raises(SetupError):
        resolve_compose(compose, {"COMPOSE_PROFILES": "other"})
