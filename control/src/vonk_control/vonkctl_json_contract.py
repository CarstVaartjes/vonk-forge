"""The ``vonkctl --json`` client contract: one model per published document.

``cluster_profiles.cli_json_contract`` decides which kind a command prints and
stamps its ``schema_version``. This module defines what each kind contains and
exports the JSON Schemas under ``schemas/vonkctl/``. A document that the
Controller already answers is that Controller response model plus the stamp, so
the CLI, the web client and third-party tools read one shape. Documents only
vonkctl prints (connection, download, observation, error) are defined here.

Presence follows docs/api-contracts.md. Optional nullable fields are omitted
when empty and are not ``required`` in the schema; required nullable fields are
an explicit ``null`` and are ``required``. Clients treat absent and ``null``
alike for the former.

Regenerate the files with ``python -m vonk_control.vonkctl_json_contract
schemas/vonkctl``; a test fails when they drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Union, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model

from cluster_profiles.cli_json_contract import DOCUMENT_VERSIONS, DocumentKind
from cluster_profiles.cli_outcome import ObservationStatus

from .artifact_jobs import ArtifactJobResponse
from .fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileEndpointsView,
    FleetProfileList,
    FleetProfilePreview,
    FleetProfileReadView,
)
from .fleet_projection import FleetSnapshot
from .library_contract import RecipeDetailResponse, RecipeLibraryResponse
from .strict_json import StrictModel

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


class ConnectionClient(StrictModel):
    version: str
    source_sha: str | None


class ConnectionDocument(StrictModel):
    """``vonkctl --check-connection --json``."""

    connected: Literal[True]
    client: ConnectionClient
    authorized_read: str
    origin: str | None


class DownloadedFile(StrictModel):
    name: str
    path: str
    size_bytes: int = Field(ge=0)
    sha256: str
    state: Literal["downloaded", "reused"]
    #: The transfer receipt of a downloaded file; absent for a reused one.
    transfer: dict | None = None


class ArtifactDownloadDocument(StrictModel):
    """``vonkctl recipe job download --json``: the verified local files."""

    job_id: str
    state: str
    output_manifest_sha256: str
    files: list[DownloadedFile]
    total_bytes: int = Field(ge=0)


class ObservationReceipt(StrictModel):
    status: ObservationStatus
    path: str
    reconnect_command: str
    timeout_seconds: float
    observed_at: str
    age_seconds: float
    reconnecting: bool
    error: str | None = None


class ObservationDocument(StrictModel):
    """A followed command: the last confirmed document and what was observed."""

    result: dict
    observation: ObservationReceipt


class ErrorDocument(BaseModel):
    """Any failure: ``error`` and ``error_type`` always, typed extras by kind."""

    model_config = ConfigDict(extra="allow")

    error: str
    error_type: str
    code: str | None = None
    detail: str | None = None
    recovery_actions: list[str] | None = None


#: Kind -> the model (or union of models) that defines it, before the stamp.
_BASES = (
    (DocumentKind.CONNECTION, ConnectionDocument),
    (DocumentKind.FLEET, FleetSnapshot),
    (DocumentKind.PROFILE_LIST, FleetProfileList),
    (DocumentKind.PROFILE, FleetProfileReadView),
    (DocumentKind.PROFILE_REVIEW, FleetProfilePreview),
    (DocumentKind.PROFILE_APPLICATION, FleetProfileApplicationView),
    (DocumentKind.PROFILE_ENDPOINTS, FleetProfileEndpointsView),
    (DocumentKind.RECIPE_LIBRARY, RecipeLibraryResponse),
    (DocumentKind.RECIPE_DETAIL, RecipeDetailResponse),
    (DocumentKind.ARTIFACT_JOB, ArtifactJobResponse),
    (DocumentKind.ARTIFACT_DOWNLOAD, ArtifactDownloadDocument),
    (DocumentKind.OBSERVATION, ObservationDocument),
    (DocumentKind.ERROR, ErrorDocument),
)


def _stamped(kind: DocumentKind, base: type[BaseModel] | UnionType) -> object:
    """``base`` (a model, or a union of models) carrying its ``schema_version``."""

    version = DOCUMENT_VERSIONS[kind]
    members = get_args(base)
    if members:
        return Union[tuple(_stamped(kind, member) for member in members)]  # noqa: UP007
    model = cast(type[BaseModel], base)
    if "schema_version" in model.model_fields:
        # A model that versions itself must agree with the table.
        if model.model_fields["schema_version"].default != version:
            raise ValueError(f"{kind} is versioned {version} but its model differs")
        return model
    return create_model(
        model.__name__ + "Document",
        __base__=model,
        __module__=__name__,
        schema_version=(cast(Any, Literal)[version], version),
    )


DOCUMENTS: dict[DocumentKind, TypeAdapter] = {
    kind: TypeAdapter(_stamped(kind, base)) for kind, base in _BASES
}
assert DOCUMENTS.keys() == DOCUMENT_VERSIONS.keys()


def schema_filename(kind: DocumentKind) -> str:
    return f"{kind}-v{DOCUMENT_VERSIONS[kind]}.schema.json"


def _open(schema: object) -> object:
    """Drop ``additionalProperties: false``: a client ignores later additive fields.

    The strict model still proves the exact shape of every document vonkctl
    prints; the published schema describes what a client may rely on.
    """

    if isinstance(schema, dict):
        return {
            key: _open(value)
            for key, value in schema.items()
            if not (key == "additionalProperties" and value is False)
        }
    if isinstance(schema, list):
        return [_open(item) for item in schema]
    return schema


def document_schema(kind: DocumentKind) -> dict:
    """The published JSON Schema of one kind."""

    schema = cast(dict, _open(DOCUMENTS[kind].json_schema()))
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["title"] = (
        f"vonkctl {kind} document (schema_version {DOCUMENT_VERSIONS[kind]})"
    )
    return schema


def schema_text(kind: DocumentKind) -> str:
    return json.dumps(document_schema(kind), indent=2, sort_keys=True) + "\n"


def write_schemas(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    expected = {schema_filename(kind) for kind in DOCUMENTS}
    for stale in directory.glob("*.schema.json"):
        if stale.name not in expected:
            stale.unlink()
    for kind in DOCUMENTS:
        (directory / schema_filename(kind)).write_text(
            schema_text(kind), encoding="utf-8"
        )


if __name__ == "__main__":
    write_schemas(Path(sys.argv[1]))
