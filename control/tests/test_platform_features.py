"""platform-features states only what this platform's own tables execute."""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

from cluster_profiles.serving_execution import (
    HttpObservation,
    ServingExecutionError,
    ServingKindNotExecuted,
    evaluate_http_response,
)
from vonk_control.interface_adapters import INTERFACE_ADAPTERS, interface_adapter
from vonk_control.model_cache.providers import PROVIDERS, model_source_kind

ROOT = Path(__file__).resolve().parents[2]
_LOADER = SourceFileLoader(
    "platform_features", str(ROOT / "scripts/recipes/platform-features")
)
_SPEC = spec_from_loader(_LOADER.name, _LOADER)
assert _SPEC is not None
assert _SPEC.loader is not None
platform_features = module_from_spec(_SPEC)
_SPEC.loader.exec_module(platform_features)

LOCK = 'package = "vonk-forge-public-contracts==5.7.0"\n'


def _has_handler(key: str, value: str) -> bool:
    """Whether the platform executes one declared value, asked of the executor."""
    if key == "serving_kinds":
        if value.endswith(".output"):
            adapter = INTERFACE_ADAPTERS.get(value.removesuffix(".output"))
            return adapter is not None and adapter.publication != "litellm"
        observation = HttpObservation(200, {}, b"{}")
        try:
            evaluate_http_response(observation, {"kind": value, "assertions": []})
        except ServingKindNotExecuted:
            return False
        except ServingExecutionError:
            return True  # an evaluator ran and refused the empty observation
        return True
    if key == "adapters":
        return interface_adapter(value).name == value
    if key == "source_providers":
        kind = model_source_kind(value)
        return kind is not None and PROVIDERS[kind].production
    return True


def test_every_declared_value_has_an_executing_handler() -> None:
    document = platform_features.features(LOCK)
    assert document["contracts_version"] == "5.7.0"
    assert document["contract_major"] == 5
    assert "openai.speech" in document["serving_kinds"]
    for key in ("serving_kinds", "adapters", "source_providers"):
        assert document[key], key
        for value in document[key]:
            assert _has_handler(key, value), (key, value)
    assert {"chat", "audio_speech"} <= set(document["api_modes"])
    assert {"levels", "text"} <= set(document["effort_kinds"])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("serving_kinds", "openai.unknown"),
        ("serving_kinds", "nothing-job.output"),
        ("source_providers", "s3"),
    ],
)
def test_a_value_without_a_handler_is_caught(key: str, value: str) -> None:
    assert not _has_handler(key, value)
