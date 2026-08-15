"""Exact schema-v1 harness lookup and projection compilation."""

from __future__ import annotations

import re
from collections.abc import Mapping

from .common import HarnessCompileError, SyntheticHarnessCompiler, validate_projection
from .contracts import HarnessCompiler, HarnessProjection

BUILTIN_HARNESS_SLUGS = (
    "vllm",
    "sglang",
    "tensorrt-llm",
    "llama-cpp",
    "ds4",
    "diffusers",
    "comfyui",
    "pytorch-pipeline",
)

_BUNDLE_MEDIA_TYPE = "application/vnd.vonk-forge.source-bundle.v1+tar"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class HarnessRegistry:
    def __init__(self) -> None:
        self._compilers: dict[str, HarnessCompiler] = {}
        self._custom_bundles: dict[str, tuple[str, int, str, str]] = {}

    @classmethod
    def with_builtins(cls) -> "HarnessRegistry":
        registry = cls()
        for slug in BUILTIN_HARNESS_SLUGS:
            registry.register(SyntheticHarnessCompiler(slug))
        return registry

    def register(
        self,
        compiler: HarnessCompiler,
        *,
        source_bundle: Mapping[str, object] | None = None,
    ) -> None:
        if not isinstance(compiler.slug, str) or not compiler.slug:
            raise HarnessCompileError("harness compiler slug is invalid")
        if compiler.contract_version != 1:
            raise HarnessCompileError("harness contract version is invalid")
        if compiler.slug in self._compilers:
            raise HarnessCompileError("harness compiler is already registered")
        if compiler.slug not in BUILTIN_HARNESS_SLUGS:
            self._custom_bundles[compiler.slug] = _signed_bundle(source_bundle)
        elif source_bundle is not None:
            raise HarnessCompileError("built-in harness does not accept a custom source bundle")
        self._compilers[compiler.slug] = compiler

    def compile(
        self,
        resolved_harness: Mapping[str, object] | object,
        recipe: Mapping[str, object],
        distribution: Mapping[str, object],
        patch: Mapping[str, object] | None,
        parameters: Mapping[str, object],
        topology: Mapping[str, object],
        role: str,
        rank: int,
    ) -> HarnessProjection:
        harness = _resolved_document(resolved_harness)
        slug, bundle = _harness_identity(harness)
        compiler = self._compilers.get(slug)
        if compiler is None:
            raise HarnessCompileError("unknown execution harness")
        if slug in self._custom_bundles:
            expected = self._custom_bundles[slug]
            actual = _unsigned_bundle(bundle)
            if actual != expected[:3]:
                raise HarnessCompileError("custom adapter lacks the exact signed source bundle")
        if distribution.get("platform") != "linux/arm64":
            raise HarnessCompileError("runtime distribution must target linux/arm64")
        projection = compiler.compile(
            recipe, distribution, patch, parameters, topology, role, rank
        )
        if projection.slug != slug:
            raise HarnessCompileError("harness compiler projection identity is invalid")
        validate_projection(projection)
        return projection


def _resolved_document(value: Mapping[str, object] | object) -> Mapping[str, object]:
    document = value if isinstance(value, Mapping) else getattr(value, "document", None)
    if not isinstance(document, Mapping):
        raise HarnessCompileError("resolved execution harness is invalid")
    return document


def _harness_identity(document: Mapping[str, object]) -> tuple[str, Mapping[str, object]]:
    identity = document.get("identity")
    bundle = document.get("source_bundle")
    if (
        document.get("schema_version") != 1
        or isinstance(document.get("schema_version"), bool)
        or document.get("kind") != "execution-harness"
        or document.get("runtime_interface") != "vonk.runtime.v1"
        or not isinstance(identity, Mapping)
        or not isinstance(identity.get("slug"), str)
        or not isinstance(bundle, Mapping)
        or set(bundle) != {"sha256", "expected_bytes", "media_type"}
    ):
        raise HarnessCompileError("resolved execution harness is invalid")
    _unsigned_bundle(bundle)
    return identity["slug"], bundle


def _unsigned_bundle(bundle: Mapping[str, object]) -> tuple[str, int, str]:
    digest = bundle.get("sha256")
    expected_bytes = bundle.get("expected_bytes")
    media_type = bundle.get("media_type")
    if (
        not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes < 1
        or media_type != _BUNDLE_MEDIA_TYPE
    ):
        raise HarnessCompileError("harness source bundle is invalid")
    return digest, expected_bytes, media_type


def _signed_bundle(bundle: Mapping[str, object] | None) -> tuple[str, int, str, str]:
    if not isinstance(bundle, Mapping):
        raise HarnessCompileError("custom adapter requires an exact signed source bundle")
    if set(bundle) != {"sha256", "expected_bytes", "media_type", "signature"}:
        raise HarnessCompileError("custom adapter requires an exact signed source bundle")
    unsigned = _unsigned_bundle(bundle)
    signature = bundle.get("signature")
    if not isinstance(signature, str) or _DIGEST.fullmatch(signature) is None:
        raise HarnessCompileError("custom adapter requires an exact signed source bundle")
    return (*unsigned, signature)
