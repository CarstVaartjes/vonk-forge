"""Stable execution-harness projection contracts for schema-v1 recipes."""

from __future__ import annotations

from dataclasses import dataclass

from ..runtime_writable_paths import EngineTelemetryContract, RuntimeWritablePath


@dataclass(frozen=True, slots=True)
class HarnessMount:
    source: str
    target: str
    read_only: bool
    isolated: bool = False


@dataclass(frozen=True, slots=True)
class HarnessBinding:
    """Exact resolved identities bound after a compiler produces a projection."""

    harness_content_sha256: str
    execution_content_sha256: str
    topology_node_count: int
    role: str
    rank: int


@dataclass(frozen=True, slots=True)
class HarnessProjection:
    """A shell-free projection for one mapped rank.

    The platform always runs linux/arm64 images as a non-root user with a
    read-only root, no capabilities and no new privileges.
    """

    slug: str
    contract_version: int
    command: tuple[str, ...]
    image: str
    network_mode: str
    user: str
    model_mounts: tuple[HarnessMount, ...]
    output_mount: HarnessMount
    input_mount: HarnessMount | None = None
    environment: tuple[tuple[str, str], ...] = ()
    writable_paths: tuple[RuntimeWritablePath, ...] = ()
    telemetry: EngineTelemetryContract | None = None
    binding: HarnessBinding | None = None
    gpu: bool = False
