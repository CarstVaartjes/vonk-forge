"""Library projection: common concerns."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel
from vonk_agent_protocol import AssetAvailability, RunState, UnknownOutcomeError
from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
)

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from ..catalog_revision_contract import read_catalog_document
from ..library_contract import (
    LibraryModelIdentity,
    LibraryRecipeSummary,
    ModelLibraryResponse,
    RecipeLibraryResponse,
)
from ..machine_states import RUN_LIVE
from ..models import CatalogDocumentRevision
from ..request_fault import RequestFault
from ..strict_json import serialize_json_value

"""Bounded canonical Model to Recipe Library projection."""


class LibraryProjectionError(UnknownOutcomeError):
    """The active catalog contains a document outside the public authority."""


class LibrarySelectorAmbiguous(ValueError):
    """A short selector names more than one canonical catalog identity."""

    def __init__(self, selector: str, candidates: Sequence[str]) -> None:
        self.selector = selector
        self.candidates = tuple(candidates)
        super().__init__(f"selector is ambiguous: {selector}")


_LIBRARY_ORDER = "catalog"


_ACTIVE_RUN_STATES = RUN_LIVE


_RUN_STATES = tuple(RunState)


_LOCAL_STATE_PRIORITY = {
    "unknown": 0,
    "not_cached": 0,
    "failed": 1,
    "preparing": 2,
    "cached": 3,
}


type LibraryControllerState = Literal[
    "cached", "preparing", "not_cached", "failed", "unknown"
]


def _wire_json_bytes(value: object) -> bytes:
    """Serialize as the compact UTF-8 JSON response used by Starlette."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _bounded_library_page[T: BaseModel](
    candidates: Sequence[T],
    *,
    has_more_after_candidates: bool,
    collection_field: Literal["models", "recipes"],
    empty_response: ModelLibraryResponse | RecipeLibraryResponse,
    encode_cursor: Callable[[T], str],
) -> tuple[list[T], str | None]:
    """Choose the largest contiguous page that fits the shared wire budget.

    The empty response includes the full filter/facet envelope. Item documents
    are encoded once and added by byte length, including the exact continuation
    token for each possible boundary. The caller's requested limit remains in
    the cursor context; this function only shortens a page when its JSON bytes
    require it.
    """

    envelope = serialize_json_value(empty_response)
    empty_items = envelope.get(collection_field)
    if empty_items != [] or envelope.get("next_cursor") is not None:
        raise AssertionError("byte page sizing requires an empty response envelope")
    envelope_bytes = len(_wire_json_bytes(envelope))
    if envelope_bytes > MAX_CONTROL_DOCUMENT_BYTES:
        raise RequestFault(
            "library response envelope requires "
            f"{envelope_bytes} bytes before entries; document limit is "
            f"{MAX_CONTROL_DOCUMENT_BYTES} bytes; narrow the library filters"
        )
    if not candidates:
        return [], None

    item_sizes = [
        len(_wire_json_bytes(serialize_json_value(item))) for item in candidates
    ]
    prefix_sizes = [0]
    for item_size in item_sizes:
        prefix_sizes.append(prefix_sizes[-1] + item_size)

    selected_count = 0
    selected_cursor: str | None = None
    first_item_response_bytes: int | None = None
    first_cursor_too_large = False
    for count in range(len(candidates), 0, -1):
        needs_cursor = count < len(candidates) or has_more_after_candidates
        try:
            cursor = encode_cursor(candidates[count - 1]) if needs_cursor else None
        except ValueError as error:
            if str(error) != "cursor document is too large":
                raise
            if count == 1:
                first_cursor_too_large = True
            # This boundary cannot be issued to a caller. A later contiguous
            # boundary can still be representable, so keep scanning prefixes.
            continue
        cursor_envelope = serialize_json_value(
            empty_response.model_copy(update={"next_cursor": cursor})
        )
        response_bytes = (
            len(_wire_json_bytes(cursor_envelope))
            + prefix_sizes[count]
            + count
            - 1  # Commas between entries.
        )
        if count == 1:
            first_item_response_bytes = response_bytes
        if response_bytes <= MAX_CONTROL_DOCUMENT_BYTES:
            selected_count = count
            selected_cursor = cursor
            break

    if selected_count == 0:
        if first_cursor_too_large:
            raise RequestFault(
                "the first matching library entry cannot be continued because "
                "its signed cursor boundary exceeds the Controller cursor limit; "
                "narrow the library filters"
            )
        assert first_item_response_bytes is not None
        raise RequestFault(
            "the first matching library entry requires "
            f"{first_item_response_bytes} bytes; document limit is "
            f"{MAX_CONTROL_DOCUMENT_BYTES} bytes; narrow filters to exclude it"
        )
    return list(candidates[:selected_count]), selected_cursor


def _controller_state(
    states: Mapping[str, LibraryControllerState], value: str, detail: str
) -> LibraryControllerState:
    """Map a persisted state onto the projected controller vocabulary."""

    controller = states.get(value)
    if controller is None:
        return cast(LibraryControllerState, AssetAvailability.UNKNOWN.value)
    return controller


def _filter_digest(filters: Mapping[str, object]) -> str:
    encoded = json.dumps(
        filters, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_LOGGER = logging.getLogger("vonk_control.library_projection")


_LOGGED_UNREADABLE: set[str] = set()


def _note_unreadable(kind: str, key: str, detail: str) -> None:
    """Say once that a stored row is unreadable.

    The projection leaves that one row out (or reads it as unknown) and goes on:
    a damaged row is evidence to rebuild, never a reason to fail the whole page.
    """

    marker = f"{kind}:{key}"
    if marker not in _LOGGED_UNREADABLE:
        _LOGGED_UNREADABLE.add(marker)
        _LOGGER.warning("ignoring unreadable %s %s: %s", kind, key, detail)


def _readable_document[T: ModelDefinition | RecipeDefinition](
    revision: CatalogDocumentRevision,
    reader: Callable[[CatalogDocumentRevision], T],
) -> T | None:
    """Retain the canonical read for this request; log an unreadable row once."""

    try:
        return reader(revision)
    except LibraryProjectionError as error:
        if revision.id not in _LOGGED_UNREADABLE:
            _LOGGED_UNREADABLE.add(revision.id)
            _LOGGER.warning(
                "skipping unreadable %s revision %s (%s/%s): %s",
                revision.kind,
                revision.id,
                revision.publisher,
                revision.slug,
                error.__cause__ or error,
            )
        return None


def _canonical_document(
    revision: CatalogDocumentRevision,
    document_type: type[ModelDefinition | RecipeDefinition],
) -> ModelDefinition | RecipeDefinition:
    try:
        parsed = read_catalog_document(revision)
        if not isinstance(parsed, document_type):
            raise TypeError("catalog document kind is unavailable")
        return parsed
    except (TypeError, ValueError) as error:
        raise LibraryProjectionError(
            f"active {revision.kind} document is not canonical"
        ) from error


def _canonical_model(revision: CatalogDocumentRevision) -> ModelDefinition:
    document = _canonical_document(revision, ModelDefinition)
    assert isinstance(document, ModelDefinition)
    return document


def _canonical_recipe(revision: CatalogDocumentRevision) -> RecipeDefinition:
    document = _canonical_document(revision, RecipeDefinition)
    assert isinstance(document, RecipeDefinition)
    return document


def _model_identity(
    revision: CatalogDocumentRevision, document: ModelDefinition
) -> LibraryModelIdentity:
    return LibraryModelIdentity(
        kind="model",
        publisher=document.identity.publisher,
        slug=document.identity.slug,
        content_sha256=revision.content_digest,
    )


def _recipe_creator(document: RecipeDefinition) -> str | None:
    """The upstream creator: the owner in the source reference, else attribution.

    The contract has no creator field and every recipe is published by the
    platform, so the owner of the source repository (for example
    ``MiaAI-Lab`` or ``nvidia``) is the creator; attribution is the fallback.
    """

    reference = document.provenance.source_reference
    if reference:
        parts = urlsplit(reference)
        owner = parts.path.strip("/").split("/", 1)[0]
        if parts.scheme in {"http", "https"} and owner:
            return owner[:128]
    for name in document.provenance.attribution:
        if name.strip():
            return name.strip()[:128]
    return None


def _canonical_recipe_summary(
    revision: CatalogDocumentRevision,
    document: RecipeDefinition,
) -> LibraryRecipeSummary:
    return LibraryRecipeSummary(
        recipe_id=revision.document_id,
        recipe_revision_id=revision.id,
        publisher=document.identity.publisher,
        slug=document.identity.slug,
        content_sha256=revision.content_digest,
        title=document.metadata.title,
        description=document.metadata.description,
        recipe_document=document,
        capabilities=[],
        topology_name=document.topology.name,
        installations=[],
        installation_total_count=0,
        installation_returned_count=0,
        installations_truncated=False,
        runs=[],
        run_total_count=0,
        run_returned_count=0,
        runs_truncated=False,
        reasons=[],
    )
