"""platform-features states only what this platform's own tables execute."""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import get_args

import pytest
from vonk_forge_contracts import ModelDefinition
from vonk_forge_contracts.recipe import ServingKind

from cluster_profiles.serving_execution import (
    HTTP_KINDS,
    HttpObservation,
    ServingExecutionError,
    ServingKindNotExecuted,
    evaluate_http_response,
)
from vonk_control.model_cache.providers import (
    PROVIDERS,
    model_source_kind,
    source_provider_word,
)

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
OBSERVATION = HttpObservation(200, {}, b"{}")


def _evaluate(kind: str) -> None:
    evaluate_http_response(OBSERVATION, {"kind": kind, "assertions": []})


def test_every_published_http_kind_reaches_its_evaluator() -> None:
    document = platform_features.features(LOCK)
    assert "openai.speech" in document["serving_kinds"]
    http = [kind for kind in document["serving_kinds"] if not kind.endswith(".output")]
    for kind in http:
        try:
            _evaluate(kind)
        except ServingKindNotExecuted:
            pytest.fail(f"{kind} is published but has no evaluator")
        except ServingExecutionError:
            pass  # its evaluator ran and refused the empty observation


def test_every_contract_http_kind_is_handled() -> None:
    contract = {kind for kind in get_args(ServingKind) if not kind.endswith(".output")}
    assert contract == HTTP_KINDS


def test_an_unknown_serving_kind_is_refused_not_evaluated_as_chat() -> None:
    with pytest.raises(ServingKindNotExecuted):
        _evaluate("openai.unknown")


def test_every_contract_model_source_downloads_through_the_provider_table() -> None:
    sources = get_args(ModelDefinition.model_fields["source"].annotation)
    assert sources
    owned = {entry.model_source for entry in PROVIDERS.values()}
    published = platform_features.features(LOCK)["source_providers"]
    for source in sources:
        assert source in owned, source
        kind = model_source_kind(source)
        assert kind is not None, source
        assert PROVIDERS[kind].production, source
        assert source_provider_word(source) in published, source
    # A class no entry names is refused, never taken for Hugging Face.
    assert model_source_kind(ModelDefinition) is None
