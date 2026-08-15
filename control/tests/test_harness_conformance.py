from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from vonk_control.catalog_contract import catalog_content_sha256
from vonk_control.harness_conformance import (
    HarnessConformanceError,
    LifecycleRequest,
    _conformance_recipe,
    _documents,
    run_synthetic_conformance,
    validate_terminal_evidence,
)
from vonk_control.harnesses import BUILTIN_HARNESS_SLUGS, HarnessRegistry
from vonk_control.harnesses.common import SyntheticHarnessCompiler
from vonk_control.harnesses.registry import TrustedBuiltinComposition


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_harness_completes_observed_synthetic_lifecycle(slug: str) -> None:
    evidence = run_synthetic_conformance(slug)

    assert evidence.phases == (
        "inspect",
        "prepare",
        "verify",
        "start",
        "inspect",
        "inspect",
        "start",
        "ready",
        "invoke",
        "inspect",
        "stop",
        "inspect",
        "inspect",
        "stop",
        "verify-stopped",
    )
    assert evidence.offline_runtime is True
    assert evidence.security["docker_socket"] is False
    assert evidence.interrupted_start_recovered is True
    assert evidence.interrupted_stop_recovered is True
    assert evidence.stop_bounded is True
    assert evidence.recovery_phases == (
        "start-interrupted",
        "inspect-idempotent",
        "start-recovered",
        "stop-interrupted",
        "inspect-idempotent",
        "stop-recovered",
    )
    assert evidence.document["schema_version"] == 1


def test_conformance_rejects_an_unrelated_driver_injection() -> None:
    with pytest.raises(TypeError):
        run_synthetic_conformance("vllm", driver_factory=object())


class BrokenConcreteProjectionCompiler:
    contract_version = 1

    def __init__(self, slug: str) -> None:
        self.slug = slug

    def compile(self, *args, **kwargs):
        projection = SyntheticHarnessCompiler(self.slug).compile(*args, **kwargs)
        return replace(projection, image="registry.example/vonk/mutable:latest")


def test_conformance_fails_for_a_broken_concrete_builtin_projection() -> None:
    compilers = tuple(
        BrokenConcreteProjectionCompiler(slug)
        if slug == "vllm"
        else SyntheticHarnessCompiler(slug)
        for slug in BUILTIN_HARNESS_SLUGS
    )
    registry = HarnessRegistry.from_trusted_builtins(
        TrustedBuiltinComposition(compilers)
    )

    with pytest.raises(HarnessConformanceError, match="digest-pinned"):
        run_synthetic_conformance("vllm", registry=registry)


def test_conformance_rejects_schema_valid_terminal_evidence_with_wrong_identity() -> (
    None
):
    harness, distribution = _documents("vllm")
    recipe = _conformance_recipe("vllm", harness, distribution)
    projection = HarnessRegistry.with_builtins().compile(
        harness,
        recipe=recipe,
        distribution=distribution,
        patch=None,
        parameters={},
        topology=recipe["topology"],
        role="entrypoint",
        rank=0,
    )
    request = LifecycleRequest(
        projection=projection,
        recipe={
            "publisher": "vonk-forge",
            "slug": "synthetic-harness",
            "content_sha256": "b" * 64,
        },
        execution_harness={
            "kind": "execution-harness",
            "publisher": "vonk-forge",
            "slug": "vllm",
            "content_sha256": catalog_content_sha256(harness),
        },
        runtime_distribution={
            "kind": "runtime-distribution",
            "publisher": "vonk-forge",
            "slug": "synthetic-arm64",
            "content_sha256": catalog_content_sha256(distribution),
        },
    )
    document = copy.deepcopy(run_synthetic_conformance("vllm").document)
    document["projection"]["image"] = "registry.example/vonk/other@sha256:" + "0" * 64

    with pytest.raises(HarnessConformanceError, match="projection identity"):
        validate_terminal_evidence(document, request)


def test_conformance_fails_closed_for_unknown_harness() -> None:
    with pytest.raises(HarnessConformanceError, match="unknown execution harness"):
        run_synthetic_conformance("legacy-harness")
