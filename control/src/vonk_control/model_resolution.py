"""Metadata-only resolution of immutable model snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,96}/[A-Za-z0-9_.-]{1,96}$")
_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt", ".pth")


class ModelResolutionError(RuntimeError):
    def __init__(self, code: str, detail: str, *, retryable: bool = False) -> None:
        self.code, self.retryable = code, retryable
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ModelFile:
    path: str
    size: int


@dataclass(frozen=True, slots=True)
class SnapshotEnvelope:
    repository: str
    revision: str
    files: tuple[ModelFile, ...]


class ModelTransport(Protocol):
    def snapshot(
        self, repository: str, revision: str, *, maximum_files: int
    ) -> SnapshotEnvelope: ...


@dataclass(frozen=True, slots=True)
class ResolvedSnapshot:
    repository: str
    revision: str
    expected_bytes: int
    weight_files: tuple[str, ...]
    auxiliary_files: tuple[str, ...]


def resolve_huggingface_snapshot(
    repository: str, revision: str, transport: ModelTransport
) -> ResolvedSnapshot:
    if _REPOSITORY.fullmatch(repository) is None:
        raise ModelResolutionError(
            "model.repository_invalid", "model repository is invalid"
        )
    if _REVISION.fullmatch(revision) is None:
        raise ModelResolutionError(
            "model.mutable_revision", "model revision must be an immutable commit"
        )
    try:
        snapshot = transport.snapshot(repository, revision, maximum_files=4096)
    except OSError as error:
        raise ModelResolutionError(
            "model.unavailable", "model provider is unavailable", retryable=True
        ) from error
    if (
        snapshot.repository != repository
        or snapshot.revision != revision
        or not 1 <= len(snapshot.files) <= 4096
    ):
        raise ModelResolutionError(
            "model.identity_changed", "model snapshot identity changed"
        )
    weights: list[str] = []
    auxiliary: list[str] = []
    total = 0
    seen: set[str] = set()
    for file in snapshot.files:
        if (
            not file.path
            or len(file.path) > 512
            or file.path.startswith("/")
            or ".." in file.path.split("/")
            or file.path in seen
            or not isinstance(file.size, int)
            or isinstance(file.size, bool)
            or file.size < 0
        ):
            raise ModelResolutionError(
                "model.file_invalid", "model file metadata is invalid"
            )
        seen.add(file.path)
        total += file.size
        (weights if file.path.lower().endswith(_WEIGHT_SUFFIXES) else auxiliary).append(
            file.path
        )
    if not weights or total < 1:
        raise ModelResolutionError(
            "model.weights_missing", "model snapshot contains no weights"
        )
    return ResolvedSnapshot(
        repository, revision, total, tuple(weights), tuple(auxiliary)
    )
