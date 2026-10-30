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
    environment: {VONK_SELECTED_PROFILES: "${COMPOSE_PROFILES:-}"}
  watcher:
    image: watcher
    profiles: [other]
    depends_on: {optional: {condition: service_started, required: false}}
"""


def test_optional_dependency_on_an_unselected_service_is_allowed() -> None:
    """Catch refusing a Compose-valid `required: false` dependency."""
    written = yaml.safe_load(
        resolve_compose(BASE, {"COMPOSE_PROFILES": "other"}).decode()
    )
    assert set(written["services"]) == {"core", "watcher"}
    assert written["services"]["watcher"]["depends_on"]["optional"]["required"] is False


def test_required_dependency_on_an_unselected_service_fails() -> None:
    """Catch writing a Compose file that cannot start."""
    compose = BASE.replace("required: false", "required: true")
    with pytest.raises(SetupError):
        resolve_compose(compose, {"COMPOSE_PROFILES": "other"})


def test_selection_reaches_the_consumer_and_a_rename_fails_loudly() -> None:
    """Catch silently leaving the selection variable unsubstituted."""
    written = yaml.safe_load(
        resolve_compose(BASE, {"COMPOSE_PROFILES": "extra"}).decode()
    )
    assert written["services"]["optional"]["environment"] == {
        "VONK_SELECTED_PROFILES": "extra"
    }
    with pytest.raises(SetupError):
        resolve_compose(
            BASE.replace("${COMPOSE_PROFILES:-}", "${PROFILES:-}"),
            {"COMPOSE_PROFILES": "extra"},
        )
