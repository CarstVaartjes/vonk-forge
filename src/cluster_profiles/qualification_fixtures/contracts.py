"""Fixture records, manifest contracts, and packaged declarations."""

from __future__ import annotations

import base64
import json
import re
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Protocol, TypedDict

from jsonschema import Draft202012Validator

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,62}/[a-z0-9][a-z0-9-]{0,62}\Z")


_SLOT = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


_CASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")


_MEDIA_TYPE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}\Z"
)


_INTERFACES = frozenset(
    {"artifact-job", "audio-job", "image-job", "mesh-job", "video-job"}
)


_FORMATS = frozenset({"glb", "json", "jpeg", "mp4", "png", "wav", "zip"})


class FixtureError(ValueError):
    """A supplied qualification manifest is unsafe or violates the contract."""


def _validate_manifest_contract(document: Mapping[str, object]) -> None:
    schema = json.loads(
        resources.files("cluster_profiles")
        .joinpath("schemas", "qualification-manifest-v2.schema.json")
        .read_text(encoding="utf-8")
    )
    # The validator's instance type is the recursive JSON alias. The manifest
    # was decoded from JSON, so round-trip it rather than asserting a type the
    # validator cannot check.
    errors = sorted(
        Draft202012Validator(schema).iter_errors(json.loads(json.dumps(document))),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        raise FixtureError(
            f"qualification manifest contract violation at {location}: {error.message}"
        )


class FixtureObservationUnknown(ValueError):
    """Missing peer output metadata; the owning case re-observes within its budget."""


class ArtifactTransferClient(Protocol):
    def download_file(
        self,
        path: str,
        destination: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        overwrite: bool,
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class Fixture:
    fixture_id: str
    path: str
    encoding: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    content: bytes
    provenance: dict[str, str] | None = None

    def declaration(self, slot: str) -> dict[str, object]:
        return {
            "slot": slot,
            "name": self.name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


class _OutputLimits(TypedDict):
    """Validated artifact-job output limits for one recipe fixture case."""

    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    allowed_media_types: list[str]


@dataclass(frozen=True, slots=True)
class RecipeFixture:
    key: str
    content_sha256: str
    interface: str
    parameters: dict[str, object]
    inputs: tuple[tuple[str, Fixture], ...]
    output_limits: _OutputLimits
    timeout_seconds: int
    assertions: tuple[dict[str, object], ...]
    case_id: str = "default"
    supplemental_cases: tuple[RecipeFixture, ...] = ()

    @property
    def all_cases(self) -> tuple[RecipeFixture, ...]:
        return (self, *self.supplemental_cases)

    @contextmanager
    def materialize(self) -> Iterator[list[tuple[dict[str, object], Path]]]:
        with tempfile.TemporaryDirectory(prefix="vonk-qualification-fixtures-") as root:
            directory = Path(root)
            values: list[tuple[dict[str, object], Path]] = []
            for slot, fixture in self.inputs:
                path = directory / fixture.name
                path.write_bytes(fixture.content)
                path.chmod(0o600)
                values.append((fixture.declaration(slot), path))
            yield values


@dataclass(frozen=True, slots=True)
class ServiceCase:
    case_id: str
    method: str
    path: str
    body: object
    timeout_seconds: int
    max_response_bytes: int
    assertions: tuple[dict[str, object], ...]

    def render(self, alias: str, fixtures: Mapping[str, Fixture]) -> dict[str, object]:
        def substitute(value: object) -> object:
            if value == "$ALIAS":
                return alias
            if isinstance(value, list):
                return [substitute(item) for item in value]
            if isinstance(value, dict):
                if set(value) in ({"$fixture_data_uri"}, {"$fixture_base64"}):
                    marker = next(iter(value))
                    fixture_id = value[marker]
                    if not isinstance(fixture_id, str) or fixture_id not in fixtures:
                        raise FixtureObservationUnknown(
                            f"service case {self.case_id} fixture is not yet observed"
                        )
                    fixture = fixtures[fixture_id]
                    encoded = base64.b64encode(fixture.content).decode("ascii")
                    if marker == "$fixture_data_uri":
                        return f"data:{fixture.media_type};base64,{encoded}"
                    return encoded
                return {key: substitute(item) for key, item in value.items()}
            return value

        return {
            "id": self.case_id,
            "method": self.method,
            "path": self.path,
            "body": substitute(self.body),
            "timeout_seconds": self.timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "assertions": [substitute(item) for item in self.assertions],
        }


@dataclass(frozen=True, slots=True)
class ServiceRecipe:
    key: str
    content_sha256: str
    alias: str
    cases: tuple[ServiceCase, ...]
    higher_tiers: dict[str, tuple[str, ...]]
