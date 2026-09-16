"""Seam checks for the platform runtime adapter identity.

The adapter is the one reviewed step that turns a built recipe image into a
Vonk runtime image.  These checks fail on the implementations the layer exists
to prevent: one flattened launcher for every engine, an adapter identity that
the digest does not cover, and an adapter that hardcodes cache paths instead of
deriving them from the central writable-path contract.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from vonk_agent_protocol import canonical_message
from vonk_control.harnesses.canonical_metadata import CANONICAL_HARNESS_BY_SLUG
from vonk_control.runtime_adapters import (
    ADAPTER_IMAGE_USER,
    RUNTIME_ADAPTER_DIGEST_LABEL,
    RUNTIME_ADAPTER_LABEL,
    RUNTIME_INTERFACE_LABEL,
    RUNTIME_INTERFACE_LABEL_VALUE,
    RuntimeAdapterError,
    render_adaptation_stage,
    resolve_runtime_adapter,
)
from vonk_control.runtime_writable_paths import writable_paths


def test_every_builtin_engine_resolves_its_own_adapter() -> None:
    adapters = {
        engine: resolve_runtime_adapter(engine, {"mode": "single"})
        for engine in CANONICAL_HARNESS_BY_SLUG
    }
    # Flattening every engine onto one launcher collapses these identities.
    assert len({adapter.adapter_id for adapter in adapters.values()}) == len(adapters)
    for engine, adapter in adapters.items():
        assert adapter.engine == engine
        assert adapter.image_user == ADAPTER_IMAGE_USER
        wrapper = CANONICAL_HARNESS_BY_SLUG[engine].wrapper
        assert wrapper in adapter.containerfile
        for other in adapters:
            foreign = CANONICAL_HARNESS_BY_SLUG[other].wrapper
            if other == engine or foreign == wrapper:
                continue
            assert foreign not in adapter.containerfile


def test_adapter_resolution_fails_closed() -> None:
    with pytest.raises(RuntimeAdapterError):
        resolve_runtime_adapter("not-an-engine")
    with pytest.raises(RuntimeAdapterError):
        resolve_runtime_adapter("vllm", {"mode": ""})
    with pytest.raises(RuntimeAdapterError):
        render_adaptation_stage("not-an-engine", launcher=None)


def test_adapter_digest_covers_the_adaptation_implementation() -> None:
    adapter = resolve_runtime_adapter("vllm", {"mode": "single"})
    assert (
        adapter.digest
        == hashlib.sha256(canonical_message(adapter.definition)).hexdigest()
    )
    # The wrong implementation digests only the adapter id, so installing a
    # different launcher under the same id would keep the same identity.
    installed = replace(
        adapter,
        containerfile=render_adaptation_stage(
            "vllm", launcher="#!/bin/sh\nexec vllm \"$@\"\n"
        ),
    )
    assert installed.adapter_id == adapter.adapter_id
    assert installed.digest != adapter.digest


def test_adapter_labels_name_the_interface_and_the_adapter_identity() -> None:
    adapter = resolve_runtime_adapter("sglang", {"mode": "distributed"})
    assert adapter.labels() == (
        (RUNTIME_INTERFACE_LABEL, RUNTIME_INTERFACE_LABEL_VALUE),
        (RUNTIME_ADAPTER_LABEL, adapter.adapter_id),
        (RUNTIME_ADAPTER_DIGEST_LABEL, adapter.digest),
    )
    wire = adapter.to_wire()
    assert wire.adapter_sha256 == adapter.digest
    assert wire.definition == adapter.definition


def test_adapter_derives_the_writable_paths_instead_of_hardcoding_them() -> None:
    vllm = resolve_runtime_adapter("vllm", {"mode": "single"}).containerfile
    llama_cpp = resolve_runtime_adapter("llama-cpp", {"mode": "single"}).containerfile
    for item in writable_paths("vllm"):
        assert item.path in vllm
    assert "/outputs/cache/vllm" in vllm
    # A hardcoded adapter list would leak one engine's paths into another.
    assert "/outputs/cache/vllm" not in llama_cpp
    assert f"USER {ADAPTER_IMAGE_USER}" in vllm
