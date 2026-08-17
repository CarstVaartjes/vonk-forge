"""Exact metadata-only artifact sizing boundary for install admission."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .recipe_contract import RecipeContractError, recipe_topology


class ArtifactSizeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactSize:
    source: str
    digest: str
    size_bytes: int


class ArtifactSizeResolver(Protocol):
    def resolve(self, recipe: Mapping[str, object]) -> tuple[ArtifactSize, ...]: ...


class StaticArtifactSizeResolver:
    def __init__(self, artifacts: tuple[ArtifactSize, ...]) -> None:
        self._artifacts = artifacts

    def resolve(self, recipe: Mapping[str, object]) -> tuple[ArtifactSize, ...]:
        source_artifacts = recipe.get("artifacts")
        if not isinstance(source_artifacts, list):
            raise ArtifactSizeError("recipe artifact identities are invalid")
        expected: set[str] = set()
        for item in source_artifacts:
            if not isinstance(item, Mapping):
                raise ArtifactSizeError("recipe artifact identity is invalid")
            expected.add(f"{item.get('repository')}@{item.get('revision')}")
        by_source = {item.source: item for item in self._artifacts}
        if set(by_source) != expected or any(
            len(item.digest) != 64 or item.size_bytes < 0 for item in by_source.values()
        ):
            raise ArtifactSizeError("artifact sizes are incomplete")
        return tuple(by_source[source] for source in sorted(by_source))


class DeclaredArtifactSizeResolver:
    """Resolve sizes already frozen into a validated immutable recipe.

    Model revisions and their declared byte counts are bound into a stable
    local identity. The built OCI image is separate install evidence because it
    does not exist until the source bundle has been built. This resolver
    performs no network I/O and therefore preserves local operation when the
    global catalog is unavailable.
    """

    def resolve(self, recipe: Mapping[str, object]) -> tuple[ArtifactSize, ...]:
        artifacts = recipe.get("artifacts")
        try:
            topology = recipe_topology(recipe)
        except RecipeContractError as error:
            raise ArtifactSizeError(
                "recipe topology artifact sizes are invalid"
            ) from error
        if not isinstance(artifacts, list):
            raise ArtifactSizeError("recipe artifact sizes are invalid")
        resolved: list[ArtifactSize] = []
        installed_by_id: dict[str, int] = {}
        seen: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                raise ArtifactSizeError("recipe artifact size is invalid")
            repository = artifact.get("repository")
            revision = artifact.get("revision")
            artifact_id = artifact.get("id")
            expected = artifact.get("installed_bytes")
            if (
                not isinstance(artifact_id, str)
                or not isinstance(repository, str)
                or not isinstance(revision, str)
                or not isinstance(expected, int)
                or isinstance(expected, bool)
                or expected < 1
            ):
                raise ArtifactSizeError("recipe artifact size is invalid")
            source = f"{repository}@{revision}"
            if source in seen:
                raise ArtifactSizeError("recipe artifact identity is duplicated")
            seen.add(source)
            identity = hashlib.sha256(f"{source}\0{expected}".encode()).hexdigest()
            resolved.append(ArtifactSize(source, identity, expected))
            installed_by_id[artifact_id] = expected
        roles = topology.get("roles")
        if not isinstance(roles, list):
            raise ArtifactSizeError("recipe topology artifact sizes are invalid")
        for role in roles:
            if not isinstance(role, Mapping) or not isinstance(
                role.get("artifacts"), list
            ):
                raise ArtifactSizeError("recipe role artifact sizes are invalid")
            resources = role.get("resources")
            disk = resources.get("disk") if isinstance(resources, Mapping) else None
            declared = disk.get("artifact_bytes") if isinstance(disk, Mapping) else None
            required = sum(
                installed_by_id.get(str(item), 0) for item in role["artifacts"]
            )
            if not isinstance(declared, int) or isinstance(declared, bool):
                raise ArtifactSizeError("recipe role artifact size is invalid")
            if declared < required:
                raise ArtifactSizeError(
                    "topology artifact size is smaller than the declared artifact total"
                )
        return tuple(sorted(resolved, key=lambda item: item.source))
