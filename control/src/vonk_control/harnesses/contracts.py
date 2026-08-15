"""Stable execution-harness compiler contracts for schema-v1 recipes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class HarnessMount:
    source: str
    target: str
    read_only: bool
    isolated: bool = False


@dataclass(frozen=True, slots=True)
class HarnessProjection:
    """A shell-free, security-complete projection for one mapped rank."""

    slug: str
    contract_version: int
    command: tuple[str, ...]
    architecture: str
    user: str
    offline_runtime: bool
    docker_socket: bool
    no_new_privileges: bool
    capabilities: tuple[str, ...]
    model_mounts: tuple[HarnessMount, ...]
    output_mount: HarnessMount


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
