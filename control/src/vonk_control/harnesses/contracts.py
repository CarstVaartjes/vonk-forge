"""Stable execution-harness compiler contracts for schema-v1 recipes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

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
    """A shell-free, security-complete projection for one mapped rank."""

    slug: str
    contract_version: int
    command: tuple[str, ...]
    image: str
    network_mode: str
    architecture: str
    user: str
    no_new_privileges: bool
    capabilities: tuple[str, ...]
    model_mounts: tuple[HarnessMount, ...]
    output_mount: HarnessMount
    input_mount: HarnessMount | None = None
    environment: tuple[tuple[str, str], ...] = ()
    writable_paths: tuple[RuntimeWritablePath, ...] = ()
    telemetry: EngineTelemetryContract | None = None
    read_only_root: bool = True
    binding: HarnessBinding | None = None
    devices: tuple[str, ...] = ()
    host_network: bool = False


class HarnessCompiler(Protocol):
    slug: str
    contract_version: int

    def compile(
        self,
        recipe: Mapping[str, object],
        distribution: Mapping[str, object],
        patch: Mapping[str, object] | None,
        parameters: Mapping[str, object],
        topology: Mapping[str, object],
        role: str,
        rank: int,
    ) -> HarnessProjection: ...
