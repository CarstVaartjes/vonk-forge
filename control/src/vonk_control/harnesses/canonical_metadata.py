"""Strict platform metadata for built-in canonical harnesses.

The recipe catalog owns ModelDefinition and RecipeDefinition documents. The
platform owns this immutable capability table because launcher, capability,
and interface policy are compiler behavior rather than catalog entities.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType

from pydantic import ConfigDict, Field
from vonk_agent_protocol.wire_model import WireModel


class CanonicalHarnessMetadata(WireModel):
    """Immutable platform contract for one built-in Recipe engine."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    adapters: tuple[str, ...] = Field(min_length=1)
    capability_requirements: tuple[str, ...] = Field(min_length=1)
    topology_modes: tuple[str, ...] = Field(min_length=1)
    security_exceptions: tuple[str, ...] = ()
    executables: tuple[str, ...] = Field(min_length=1)
    wrapper: str = Field(pattern=r"^/[A-Za-z0-9._/-]+$")

    @property
    def content_sha256(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


CANONICAL_HARNESSES: tuple[CanonicalHarnessMetadata, ...] = (
    CanonicalHarnessMetadata(
        slug="vllm",
        adapters=("openai",),
        capability_requirements=("nvidia-gpu",),
        topology_modes=("single", "distributed"),
        security_exceptions=("model.trust-remote-code",),
        executables=("vllm", "vllm-serve"),
        wrapper="/opt/vonk/bin/vllm",
    ),
    CanonicalHarnessMetadata(
        slug="sglang",
        adapters=("openai",),
        capability_requirements=("nvidia-gpu",),
        topology_modes=("single", "distributed"),
        security_exceptions=("model.trust-remote-code",),
        executables=("sglang", "sglang-serve"),
        wrapper="/opt/vonk/bin/sglang-serve",
    ),
    CanonicalHarnessMetadata(
        slug="tensorrt-llm",
        adapters=("openai",),
        capability_requirements=("nvidia-gpu",),
        topology_modes=("single",),
        security_exceptions=(),
        executables=("trtllm-serve", "tensorrt-llm"),
        wrapper="/usr/local/bin/trtllm-serve",
    ),
    CanonicalHarnessMetadata(
        slug="llama-cpp",
        adapters=("openai",),
        capability_requirements=("nvidia-gpu",),
        topology_modes=("single",),
        security_exceptions=(),
        executables=("llama-server", "llama-cpp"),
        wrapper="/opt/vonk/bin/llama-server",
    ),
    CanonicalHarnessMetadata(
        slug="ds4",
        adapters=("openai",),
        capability_requirements=("nvidia-gpu",),
        topology_modes=("single",),
        security_exceptions=(),
        executables=("ds4-serve", "ds4"),
        wrapper="/opt/vonk/bin/ds4-serve",
    ),
    CanonicalHarnessMetadata(
        slug="diffusers",
        adapters=("image-job", "audio-job", "video-job", "artifact-job"),
        capability_requirements=("nvidia-gpu",),
        topology_modes=("single",),
        security_exceptions=(),
        executables=("diffusers-job", "diffusers"),
        wrapper="/opt/vonk/bin/diffusers-job",
    ),
    CanonicalHarnessMetadata(
        slug="comfyui",
        adapters=("image-job", "audio-job", "video-job", "artifact-job"),
        capability_requirements=("nvidia-gpu", "immutable-workflow"),
        topology_modes=("single",),
        security_exceptions=(),
        executables=("comfyui-job", "comfyui"),
        wrapper="/opt/vonk/bin/comfyui-job",
    ),
    CanonicalHarnessMetadata(
        slug="pytorch-pipeline",
        adapters=("image-job", "audio-job", "video-job", "mesh-job", "artifact-job"),
        capability_requirements=("nvidia-gpu", "signed-source-bundle"),
        topology_modes=("single",),
        security_exceptions=(),
        executables=("pytorch-pipeline", "pytorch"),
        wrapper="/opt/vonk/bin/pytorch-pipeline",
    ),
)

CANONICAL_HARNESS_BY_SLUG: Mapping[str, CanonicalHarnessMetadata] = MappingProxyType(
    {metadata.slug: metadata for metadata in CANONICAL_HARNESSES}
)


def canonical_harness(slug: str) -> CanonicalHarnessMetadata:
    """Resolve a built-in engine's immutable platform metadata."""

    try:
        return CANONICAL_HARNESS_BY_SLUG[slug]
    except KeyError as error:
        raise ValueError(f"unknown canonical harness: {slug}") from error


__all__ = [
    "CANONICAL_HARNESSES",
    "CANONICAL_HARNESS_BY_SLUG",
    "CanonicalHarnessMetadata",
    "canonical_harness",
]
