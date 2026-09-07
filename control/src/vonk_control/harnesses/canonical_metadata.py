"""Versioned platform metadata for the built-in canonical harnesses.

The recipe catalog owns ModelDefinition and RecipeDefinition documents.  The
platform owns this small, immutable capability table because launcher,
security, and interface policy are compiler behavior rather than catalog
entities.  Keep the document projection stable for legacy evidence consumers;
it is derived from these typed values and is never read from disk.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class CanonicalHarnessMetadata:
    """Immutable platform contract for one built-in Recipe engine."""

    slug: str
    adapters: tuple[str, ...]
    capability_requirements: tuple[str, ...]
    topology_modes: tuple[str, ...]
    security_exceptions: tuple[str, ...]
    executables: tuple[str, ...]
    wrapper: str
    title: str
    description: str
    tags: tuple[str, ...]
    publisher: str = "vonk-forge"
    contract_version: int = 1
    runtime_interface: str = "vonk.runtime.v1"
    schema_version: int = 1

    @property
    def identity(self) -> tuple[str, str]:
        return self.publisher, self.slug

    @property
    def compiler_slug(self) -> str:
        return self.slug

    def document(self) -> dict[str, object]:
        """Return the stable evidence projection, detached from this object."""

        return {
            "adapters": list(self.adapters),
            "capability_requirements": list(self.capability_requirements),
            "compiler_slug": self.compiler_slug,
            "contract_version": self.contract_version,
            "identity": {"publisher": self.publisher, "slug": self.slug},
            "kind": "execution-harness",
            "metadata": {
                "description": self.description,
                "tags": list(self.tags),
                "title": self.title,
            },
            "runtime_interface": self.runtime_interface,
            "schema_version": self.schema_version,
            "security_exceptions": list(self.security_exceptions),
            "topology_modes": list(self.topology_modes),
        }

    def document_bytes(self) -> bytes:
        """Return canonical bytes used for stable harness identity evidence."""

        return (
            json.dumps(
                self.document(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.document_bytes()).hexdigest()


CANONICAL_HARNESSES: tuple[CanonicalHarnessMetadata, ...] = (
    CanonicalHarnessMetadata(
        slug="vllm",
        adapters=("openai",),
        capability_requirements=("nvidia-gpu",),
        topology_modes=("single",),
        security_exceptions=("model.trust-remote-code",),
        executables=("vllm", "vllm-serve"),
        wrapper="/opt/vonk/bin/vllm",
        title="vLLM execution harness",
        description="Target-driven shell-free single-node vLLM serving execution harness.",
        tags=("builtin", "inference", "vllm"),
    ),
    CanonicalHarnessMetadata(
        slug="sglang",
        adapters=("openai",),
        capability_requirements=("nvidia-gpu",),
        topology_modes=("single", "distributed"),
        security_exceptions=("model.trust-remote-code",),
        executables=("sglang", "sglang-serve"),
        wrapper="/opt/vonk/bin/sglang-serve",
        title="SGLang execution harness",
        description="Target-driven shell-free single-node and verified native distributed SGLang serving execution harness.",
        tags=("builtin", "distributed", "inference", "sglang"),
    ),
    CanonicalHarnessMetadata(
        slug="tensorrt-llm",
        adapters=("openai",),
        capability_requirements=("nvidia-gpu",),
        topology_modes=("single",),
        security_exceptions=(),
        executables=("trtllm-serve", "tensorrt-llm"),
        wrapper="/usr/local/bin/trtllm-serve",
        title="TensorRT-LLM execution harness",
        description="Target-driven shell-free single-node TensorRT-LLM serving execution harness.",
        tags=("builtin", "inference", "tensorrt-llm"),
    ),
    CanonicalHarnessMetadata(
        slug="llama-cpp",
        adapters=("openai",),
        capability_requirements=("nvidia-gpu",),
        topology_modes=("single",),
        security_exceptions=(),
        executables=("llama-server", "llama-cpp"),
        wrapper="/opt/vonk/bin/llama-server",
        title="llama.cpp execution harness",
        description="Target-driven shell-free llama.cpp GGUF serving execution harness.",
        tags=("builtin", "inference", "llama-cpp"),
    ),
    CanonicalHarnessMetadata(
        slug="ds4",
        adapters=("openai",),
        capability_requirements=("nvidia-gpu",),
        topology_modes=("single",),
        security_exceptions=(),
        executables=("ds4-serve", "ds4"),
        wrapper="/opt/vonk/bin/ds4-serve",
        title="DS4 execution harness",
        description="Target-driven shell-free single-node DS4 harness with an optional DSpark drafter.",
        tags=("builtin", "ds4", "inference"),
    ),
    CanonicalHarnessMetadata(
        slug="diffusers",
        adapters=("image-job", "audio-job", "video-job", "artifact-job"),
        capability_requirements=("nvidia-gpu",),
        topology_modes=("single", "data_parallel"),
        security_exceptions=(),
        executables=("diffusers-job", "diffusers"),
        wrapper="/opt/vonk/bin/diffusers-job",
        title="Diffusers execution harness",
        description="Target-driven shell-free Diffusers artifact-job execution harness.",
        tags=("artifact-job", "builtin", "diffusers"),
    ),
    CanonicalHarnessMetadata(
        slug="comfyui",
        adapters=("image-job", "audio-job", "video-job", "artifact-job"),
        capability_requirements=("nvidia-gpu", "immutable-workflow"),
        topology_modes=("single", "data_parallel"),
        security_exceptions=(),
        executables=("comfyui-job", "comfyui"),
        wrapper="/opt/vonk/bin/comfyui-job",
        title="ComfyUI execution harness",
        description="Target-driven shell-free immutable ComfyUI workflow execution harness.",
        tags=("artifact-job", "builtin", "comfyui"),
    ),
    CanonicalHarnessMetadata(
        slug="pytorch-pipeline",
        adapters=("image-job", "audio-job", "video-job", "mesh-job", "artifact-job"),
        capability_requirements=("nvidia-gpu", "signed-source-bundle"),
        topology_modes=("single", "data_parallel"),
        security_exceptions=(),
        executables=("pytorch-pipeline", "pytorch"),
        wrapper="/opt/vonk/bin/pytorch-pipeline",
        title="PyTorch pipeline execution harness",
        description="Target-driven shell-free signed-bundle PyTorch pipeline execution harness.",
        tags=("artifact-job", "builtin", "pytorch"),
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
