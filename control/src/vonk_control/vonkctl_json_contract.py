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
from typing import Annotated, Any, Literal, Optional, Union, cast, get_args

from pydantic import BaseModel, Field, TypeAdapter, create_model

from cluster_profiles.cli_json_contract import (
    DOCUMENT_VERSIONS,
    SELF_VERSIONED,
    CliErrorType,
    DocumentKind,
)
from cluster_profiles.cli_outcome import ObservationStatus, SubmissionAcceptance
from cluster_profiles.control_client.transfers import TransferReceipt
from cluster_profiles.error_reporting import (
    CODE_PATTERN,
    ErrorDecision,
    ErrorSource,
    TransportKind,
)

from .artifact_jobs import ArtifactJobResponse
from .artifact_jobs.contracts import ArtifactJobState
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

#: The shared code grammar as a JSON Schema pattern (``\\Z`` is Python-only).
_SCHEMA_CODE_PATTERN = "^" + CODE_PATTERN.replace("\\Z", "$")
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
    #: The transfer client's receipt of a downloaded file; absent for a reused one.
    transfer: TransferReceipt | None = None


class ArtifactDownloadDocument(StrictModel):
    """``vonkctl recipe job download --json``: the verified local files."""

    job_id: str
    state: ArtifactJobState
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


class Reconcile(StrictModel):
    """The command that settles an outcome this process could not confirm."""

    operation: str
    request_key: str | None = None
    artifact_job_id: str | None = None


class ErrorContextFields(StrictModel):
    """The safe context of a surfaced failure (``ErrorContext.as_dict``)."""

    operation: str
    code: str = Field(pattern=_SCHEMA_CODE_PATTERN)
    source: ErrorSource
    decision: ErrorDecision
    retryable: bool
    endpoint: str | None = None
    http_status: int | None = None
    request_id: str | None = None
    transport: TransportKind | None = None
    errno: int | None = None
    path: str | None = None


class SubmissionFailure(ErrorContextFields):
    """One failed attempt of a submission: its stage, context and Controller detail."""

    stage: str
    detail: str | None = None


class SubmissionReceipt(StrictModel):
    """What the process knows about acceptance, never about remote execution."""

    request_key: str
    path: str
    lookup_path: str
    timeout_seconds: float
    action: str
    acceptance: SubmissionAcceptance
    operation_id: str | None = None
    failures: list[SubmissionFailure]


class UploadedInput(StrictModel):
    name: str
    sha256: str


class NothingConfirmed(StrictModel):
    """A followed command stopped before the Controller confirmed anything."""


class ObservationDocument(StrictModel):
    """A followed command: what was observed, and the last confirmed result.

    The concrete ``result`` and its ``result_kind`` tag are added per kind below.
    """

    observation: ObservationReceipt
    submission: SubmissionReceipt | None = None
    request_key: str | None = None


class ErrorDocument(StrictModel):
    """Any failure. ``error`` and ``error_type`` are always present.

    Fields beyond them appear by cause: the Controller problem (``code``,
    ``detail``, retry hints), the error context, an unconfirmed submission, the
    command that reconciles it, and the artifact-job progress made so far. An
    ended followed command also carries ``observation``, ``result_kind`` and
    ``result`` (added per kind below).
    """

    error: str
    error_type: CliErrorType
    code: str | None = Field(default=None, pattern=_SCHEMA_CODE_PATTERN)
    detail: str | None = None
    recovery_actions: list[str] | None = None
    usage: str | None = None
    retryable: bool | None = None
    retry_time: str | None = None
    retry_after_seconds: int | None = None
    preserved: str | None = None
    required_bytes: int | None = None
    free_bytes: int | None = None
    shortfall_bytes: int | None = None
    log_excerpt: str | None = None
    candidates: list[str] | None = None
    operation: str | None = None
    endpoint: str | None = None
    http_status: int | None = None
    request_id: str | None = None
    source: ErrorSource | None = None
    transport: TransportKind | None = None
    decision: ErrorDecision | None = None
    errno: int | None = None
    path: str | None = None
    submission: SubmissionReceipt | None = None
    request_key: str | None = None
    reconcile: Reconcile | None = None
    artifact_job_id: str | None = None
    uploaded_inputs: list[UploadedInput] | None = None
    upload_acceptance: Literal["unknown"] | None = None
    input_name: str | None = None
    downloaded_files: list[DownloadedFile] | None = None
    partial_collection: bool | None = None
    downloaded_file_count: int | None = None
    expected_file_count: int | None = None
    observation: ObservationReceipt | None = None


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
)
_BASES_BY_KIND = dict(_BASES)
_WRAPPERS = (DocumentKind.OBSERVATION, DocumentKind.ERROR)
assert set(_BASES_BY_KIND) | set(_WRAPPERS) == set(DocumentKind)
assert set(DOCUMENT_VERSIONS) | SELF_VERSIONED == set(DocumentKind)
#: The kinds a followed command can leave in ``result``.
_RESULT_KINDS = tuple(
    kind for kind in _BASES_BY_KIND if kind != DocumentKind.CONNECTION
)


def schema_version(kind: DocumentKind) -> int:
    """The version of a kind: the CLI table, or the model that versions itself."""

    if kind in SELF_VERSIONED:
        model = cast(type[BaseModel], _BASES_BY_KIND[kind])
        return cast(int, model.model_fields["schema_version"].default)
    return DOCUMENT_VERSIONS[kind]


def _stamped(kind: DocumentKind, base: Any) -> Any:
    """``base`` (a model, or a union of models) with a required ``schema_version``."""

    members = get_args(base)
    if members:
        return Union[tuple(_stamped(kind, member) for member in members)]  # noqa: UP007
    return create_model(
        base.__name__ + "Document",
        __base__=base,
        __module__=__name__,
        schema_version=(cast(Any, Literal)[schema_version(kind)], ...),
    )


_STAMPED = {kind: _stamped(kind, base) for kind, base in _BASES}
_OBSERVED = {
    kind: Union[_STAMPED[kind], NothingConfirmed]  # noqa: UP007
    for kind in _RESULT_KINDS
}


def _title(kind: DocumentKind) -> str:
    return "".join(part.title() for part in kind.split("-"))


def _wrapper(kind: DocumentKind) -> Any:
    """The observation wrapper (tagged by ``result_kind``) or the failure document."""

    stamp = (cast(Any, Literal)[DOCUMENT_VERSIONS[kind]], ...)
    if kind == DocumentKind.OBSERVATION:
        variants = [
            create_model(
                "Observed" + _title(result_kind),
                __base__=ObservationDocument,
                __module__=__name__,
                schema_version=stamp,
                result_kind=(cast(Any, Literal)[result_kind], ...),
                result=(_OBSERVED[result_kind], ...),
            )
            for result_kind in _RESULT_KINDS
        ]
        return Annotated[Union[tuple(variants)], Field(discriminator="result_kind")]  # noqa: UP007
    return create_model(
        "ErrorDocumentStamped",
        __base__=ErrorDocument,
        __module__=__name__,
        schema_version=stamp,
        result_kind=(DocumentKind | None, None),
        result=(Optional[Union[tuple(_OBSERVED.values())]], None),  # noqa: UP007, UP045
    )


DOCUMENTS: dict[DocumentKind, TypeAdapter] = {
    **{kind: TypeAdapter(model) for kind, model in _STAMPED.items()},
    **{kind: TypeAdapter(_wrapper(kind)) for kind in _WRAPPERS},
}
assert DOCUMENTS.keys() == set(DocumentKind)


def schema_filename(kind: DocumentKind) -> str:
    return f"{kind}-v{schema_version(kind)}.schema.json"


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
    schema["title"] = f"vonkctl {kind} document (schema_version {schema_version(kind)})"
    return schema


def schema_text(kind: DocumentKind) -> str:
    return json.dumps(document_schema(kind), indent=2, sort_keys=True) + "\n"


def incompatibilities(old: object, new: object, path: str = "") -> list[str]:
    """What a client of ``old`` can no longer rely on in ``new``.

    A removed property, a ``required`` list that grew, a changed ``type`` or a
    shorter alternative list all need a new ``schema_version``; added
    properties and added alternatives do not. A ``$ref`` that became a
    ``oneOf``/``anyOf`` of references is an added alternative, not a removal.
    """

    problems: list[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key, value in old.items():
            where = f"{path}/{key}"
            if key not in new:
                if path.endswith("/properties"):
                    problems.append(f"{where}: property removed")
                elif key == "$ref" and ("oneOf" in new or "anyOf" in new):
                    # A reference that became a union of references (a type
                    # that gained a member) keeps its old shape as a member;
                    # the alternatives are compared where they sit.
                    pass
                elif key in ("type", "const", "$ref"):
                    problems.append(f"{where}: removed")
                continue
            if key == "required":
                grown = sorted(set(new[key]) - set(value))
                if grown:
                    problems.append(f"{where}: now required {grown}")
            elif key in ("type", "const", "$ref"):
                if value != new[key]:
                    problems.append(f"{where}: {value!r} became {new[key]!r}")
            else:
                problems.extend(incompatibilities(value, new[key], where))
    elif isinstance(old, list) and isinstance(new, list):
        if len(new) < len(old) and path.rsplit("/", 1)[-1] in ("anyOf", "oneOf"):
            problems.append(f"{path}: alternatives removed")
        for index, (before, after) in enumerate(zip(old, new, strict=False)):
            problems.extend(incompatibilities(before, after, f"{path}/{index}"))
    return problems


def write_schemas(directory: Path) -> None:
    """Regenerate the published schemas; refuse an incompatible change in place.

    A file is named by its version, so an incompatible change must bump the
    version (a new file name) instead of rewriting the one clients rely on.
    """

    directory.mkdir(parents=True, exist_ok=True)
    expected = {schema_filename(kind) for kind in DOCUMENTS}
    for kind in DOCUMENTS:
        target = directory / schema_filename(kind)
        if target.exists():
            problems = incompatibilities(
                json.loads(target.read_text()), document_schema(kind)
            )
            if problems:
                raise SystemExit(
                    f"{target.name}: incompatible change without a version bump:\n"
                    + "\n".join(problems)
                )
    for stale in directory.glob("*.schema.json"):
        if stale.name not in expected:
            stale.unlink()
    for kind in DOCUMENTS:
        (directory / schema_filename(kind)).write_text(
            schema_text(kind), encoding="utf-8"
        )


if __name__ == "__main__":
    write_schemas(Path(sys.argv[1]))
