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

This is a repository tool, not Controller code: it needs both the CLI vocabulary
and the server models, so the server image never imports it. Every definition lives once in
``definitions.schema.json``; a kind's file holds its root and ``$ref``s into it.
Regenerate the files
with ``uv run --project control --frozen python tools/vonkctl_json_contract.py
schemas/vonkctl``; a test fails when they drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Union, cast, get_args

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, TypeAdapter, create_model
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

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
from vonk_control.artifact_jobs.contracts import ArtifactJobResponse, ArtifactJobState
from vonk_control.fleet_profile_contract.definitions import (
    FleetProfileList,
    FleetProfileReadView,
)
from vonk_control.fleet_profile_contract.endpoints import FleetProfileEndpointsView
from vonk_control.fleet_profile_contract.review import (
    FleetProfileApplicationView,
    FleetProfilePreview,
)
from vonk_control.fleet_projection.common import FleetSnapshot
from vonk_control.library.contract import RecipeDetailResponse, RecipeLibraryResponse
from vonk_control.strict_json import StrictModel

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
    """``vonkctl job download --json``: the verified local files."""

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


DOCUMENTS: dict[DocumentKind, TypeAdapter[Any]] = {
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


DEFINITIONS_FILENAME = "definitions.schema.json"
_DEFS_PREFIX = "#/$defs/"


def _bundled_schema(kind: DocumentKind) -> dict[str, Any]:
    """One kind as pydantic exports it: its root plus every definition it uses."""

    schema = cast(dict[str, Any], _open(DOCUMENTS[kind].json_schema()))
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["title"] = f"vonkctl {kind} document (schema_version {schema_version(kind)})"
    return schema


def _retarget(schema: object, old: str, new: str) -> object:
    """``schema`` with every ``$ref`` that starts with ``old`` starting with ``new``."""

    if isinstance(schema, dict):
        return {
            key: (
                new + value[len(old) :]
                if key == "$ref" and isinstance(value, str) and value.startswith(old)
                else _retarget(value, old, new)
            )
            for key, value in schema.items()
        }
    if isinstance(schema, list):
        return [_retarget(item, old, new) for item in schema]
    return schema


def definitions_schema() -> dict[str, Any]:
    """Every definition of every kind, once. A name means one definition."""

    definitions: dict[str, dict[str, Any]] = {}
    for kind in DOCUMENTS:
        for name, definition in _bundled_schema(kind).get("$defs", {}).items():
            if definitions.setdefault(name, definition) != definition:
                raise SystemExit(f"{name}: two kinds define it differently")
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "title": "vonkctl shared definitions",
        "$defs": definitions,
    }


def document_schema(kind: DocumentKind) -> dict[str, Any]:
    """The published JSON Schema of one kind: its root, definitions by reference."""

    schema = _bundled_schema(kind)
    schema.pop("$defs", None)
    return cast(
        dict[str, Any],
        _retarget(schema, _DEFS_PREFIX, f"{DEFINITIONS_FILENAME}{_DEFS_PREFIX}"),
    )


def bundled(published: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any]:
    """A published kind with its definitions inlined as local ``$defs``."""

    schema = cast(
        dict[str, Any],
        _retarget(published, f"{DEFINITIONS_FILENAME}{_DEFS_PREFIX}", _DEFS_PREFIX),
    )
    return {**schema, "$defs": definitions["$defs"]}


def schema_registry(directory: Path) -> Any:
    """A ``referencing`` registry holding every published file by file name."""

    return Registry().with_resources(
        (
            path.name,
            Resource.from_contents(
                json.loads(path.read_text()), default_specification=DRAFT202012
            ),
        )
        for path in directory.glob("*.schema.json")
    )


def validator(kind: DocumentKind, directory: Path) -> Any:
    """A validator of one published kind that resolves its shared definitions."""

    schema = json.loads((directory / schema_filename(kind)).read_text())
    return Draft202012Validator(schema, registry=schema_registry(directory))


def schema_text(kind: DocumentKind) -> str:
    return _text(document_schema(kind))


def definitions_text() -> str:
    return _text(definitions_schema())


def _text(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def _definition(root: object, ref: object) -> object:
    name = ref.removeprefix("#/$defs/") if isinstance(ref, str) else ""
    defs = root.get("$defs") if isinstance(root, dict) else None
    return defs.get(name) if isinstance(defs, dict) else None


def incompatibilities(
    old: object,
    new: object,
    path: str = "",
    roots: tuple[object, object] | None = None,
    seen: frozenset[tuple[object, object]] = frozenset(),
) -> list[str]:
    """What a client of ``old`` can no longer rely on in ``new``.

    A removed property, a ``required`` list that grew or a changed ``type`` needs
    a new ``schema_version``; added properties and added or removed
    alternatives do not. A ``$ref`` that now names another definition is
    compatible when that definition is the old one plus compatible additions
    (it passes the same check), so a type may gain optional fields as a subtype.
    """

    roots = roots or (old, new)
    problems: list[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key, value in old.items():
            where = f"{path}/{key}"
            if key not in new:
                if path.endswith("/properties"):
                    problems.append(f"{where}: property removed")
                elif key in ("type", "const", "$ref"):
                    problems.append(f"{where}: removed")
                continue
            if key == "required":
                grown = sorted(set(new[key]) - set(value))
                if grown:
                    problems.append(f"{where}: now required {grown}")
            elif key in ("type", "const", "$ref"):
                if value == new[key]:
                    continue
                before = _definition(roots[0], value) if key == "$ref" else None
                after = _definition(roots[1], new[key]) if key == "$ref" else None
                pair = (value, new[key])
                if (
                    before is not None
                    and after is not None
                    and (
                        pair in seen
                        or not incompatibilities(
                            before, after, where, roots, seen | {pair}
                        )
                    )
                ):
                    # A pair already being compared is compatible (recursive defs).
                    continue
                problems.append(f"{where}: {value!r} became {new[key]!r}")
            else:
                problems.extend(incompatibilities(value, new[key], where, roots, seen))
    elif isinstance(old, list) and isinstance(new, list):
        if path.rsplit("/", 1)[-1] in ("anyOf", "oneOf"):
            # Alternatives pair by ``$ref``; a dropped one is never emitted.
            refs = {
                item["$ref"]: item
                for item in new
                if isinstance(item, dict) and "$ref" in item
            }
            pairs = [
                (before, refs[before["$ref"]])
                for before in old
                if isinstance(before, dict) and before.get("$ref") in refs
            ] + [
                pair
                for pair in zip(old, new, strict=False)
                if not (isinstance(pair[0], dict) and "$ref" in pair[0])
            ]
        else:
            pairs = list(zip(old, new, strict=False))
        for index, (before, after) in enumerate(pairs):
            problems.extend(
                incompatibilities(before, after, f"{path}/{index}", roots, seen)
            )
    return problems


def write_schemas(directory: Path) -> None:
    """Regenerate the published schemas; refuse an incompatible change in place.

    A file is named by its version, so an incompatible change must bump the
    version (a new file name) instead of rewriting the one clients rely on.
    """

    directory.mkdir(parents=True, exist_ok=True)
    expected = {schema_filename(kind) for kind in DOCUMENTS} | {DEFINITIONS_FILENAME}
    shared = definitions_schema()
    previous = directory / DEFINITIONS_FILENAME
    old_definitions = json.loads(previous.read_text()) if previous.exists() else None
    for kind in DOCUMENTS:
        target = directory / schema_filename(kind)
        if target.exists():
            old = json.loads(target.read_text())
            if "$defs" not in old and old_definitions is not None:
                old = bundled(old, old_definitions)
            problems = incompatibilities(old, bundled(document_schema(kind), shared))
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
    previous.write_text(definitions_text(), encoding="utf-8")


if __name__ == "__main__":
    write_schemas(Path(sys.argv[1]))
