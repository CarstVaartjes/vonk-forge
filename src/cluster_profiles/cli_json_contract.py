"""The versioned ``vonkctl --json`` client contract.

Every published ``--json`` document carries ``schema_version``. The version
belongs to the document kind, never to the vonkctl release: it changes only when
a field is removed, renamed or changes type (an additive field keeps it). The
kinds that third-party clients consume are the ``DocumentKind`` members; each is
defined by one Pydantic model in ``vonk_control.vonkctl_json_contract`` whose
JSON Schema is published under ``schemas/vonkctl/``. Any other document is not
part of the client contract: it carries no ``schema_version`` and no promise.

Presence follows the repository rule (docs/api-contracts.md): an optional
nullable field is omitted when it has no value, and a required nullable field is
an explicit ``null``. The published schemas say which is which.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from enum import StrEnum


class DocumentKind(StrEnum):
    CONNECTION = "connection"
    FLEET = "fleet"
    PROFILE_LIST = "profile-list"
    PROFILE = "profile"
    PROFILE_REVIEW = "profile-review"
    PROFILE_APPLICATION = "profile-application"
    PROFILE_ENDPOINTS = "profile-endpoints"
    RECIPE_LIBRARY = "recipe-library"
    RECIPE_DETAIL = "recipe-detail"
    ARTIFACT_JOB = "artifact-job"
    ARTIFACT_DOWNLOAD = "artifact-download"
    OBSERVATION = "observation"
    ERROR = "error"


#: The command words a kind is chosen by (``argparse`` destinations' values).
class _Word(StrEnum):
    FLEET = "fleet"
    PROFILE = "profile"
    RECIPE = "recipe"
    LIST = "list"
    ENDPOINT = "endpoint"
    LOAD = "load"
    PROGRESS = "progress"
    CANCEL = "cancel"
    LIBRARY = "library"
    DETAIL = "detail"
    JOB = "job"
    DOWNLOAD = "download"
    CREATE = "create"
    UPLOAD = "upload"
    SUBMIT = "submit"
    RESULT = "result"


class ErrorKey(StrEnum):
    """The two keys every failure document carries."""

    ERROR = "error"
    ERROR_TYPE = "error_type"


class CliErrorType(StrEnum):
    """Where a failure document comes from (``error_type``)."""

    ARGUMENTS = "arguments"
    CONTROL_API = "control_api"
    DECLINED = "declined"
    UPDATE = "update"
    ENROLLMENT_DELIVERY = "enrollment_delivery"


#: Kinds whose Controller model owns its ``schema_version``; the stamp never
#: sets it and this module holds no second copy of the number.
SELF_VERSIONED = frozenset({DocumentKind.PROFILE_REVIEW})

#: Kind -> schema_version, for every kind the CLI stamps.
DOCUMENT_VERSIONS: Mapping[DocumentKind, int] = {
    DocumentKind.CONNECTION: 1,
    DocumentKind.FLEET: 1,
    DocumentKind.PROFILE_LIST: 1,
    DocumentKind.PROFILE: 1,
    DocumentKind.PROFILE_APPLICATION: 1,
    DocumentKind.PROFILE_ENDPOINTS: 1,
    DocumentKind.RECIPE_LIBRARY: 1,
    DocumentKind.RECIPE_DETAIL: 1,
    DocumentKind.ARTIFACT_JOB: 1,
    DocumentKind.ARTIFACT_DOWNLOAD: 1,
    DocumentKind.OBSERVATION: 1,
    DocumentKind.ERROR: 1,
}

_PROFILE_APPLICATION_ACTIONS = frozenset({_Word.LOAD, _Word.PROGRESS, _Word.CANCEL})
_ARTIFACT_JOB_ACTIONS = frozenset(
    {_Word.CREATE, _Word.UPLOAD, _Word.SUBMIT, _Word.DETAIL, _Word.CANCEL}
)


def document_kind(
    args: argparse.Namespace, document: Mapping, *, error: bool
) -> DocumentKind | None:
    """The published kind of the document this invocation prints, if any."""

    if error:
        # Enrollment receipts are their own documents, not the failure contract.
        unpublished = document.get("error_type") == CliErrorType.ENROLLMENT_DELIVERY
        return None if unpublished else DocumentKind.ERROR
    if getattr(args, "check_connection", False):
        return DocumentKind.CONNECTION
    # A followed command's wrapper holds ``result`` beside an ``observation`` receipt.
    if DocumentKind.OBSERVATION in document and _Word.RESULT in document:
        return DocumentKind.OBSERVATION
    command = getattr(args, "command", None)
    if command == _Word.FLEET:
        return (
            DocumentKind.FLEET if getattr(args, "fleet_action", None) is None else None
        )
    if command == _Word.PROFILE:
        action = getattr(args, "profile_action", None)
        if action is None:
            return DocumentKind.PROFILE
        if action == _Word.LIST:
            return DocumentKind.PROFILE_LIST
        if action == _Word.ENDPOINT:
            return DocumentKind.PROFILE_ENDPOINTS
        if action == _Word.LOAD and getattr(args, "dry_run", False):
            return DocumentKind.PROFILE_REVIEW
        if action in _PROFILE_APPLICATION_ACTIONS:
            return DocumentKind.PROFILE_APPLICATION
        return None
    if command == _Word.RECIPE:
        action = getattr(args, "recipe_action", None)
        if action in (None, _Word.LIBRARY):
            return DocumentKind.RECIPE_LIBRARY
        if action == _Word.DETAIL:
            return DocumentKind.RECIPE_DETAIL
        if action == _Word.JOB:
            job_action = getattr(args, "recipe_job_action", None)
            if job_action == _Word.DOWNLOAD:
                return DocumentKind.ARTIFACT_DOWNLOAD
            if job_action in _ARTIFACT_JOB_ACTIONS:
                return DocumentKind.ARTIFACT_JOB
    return None


def stamp(document: Mapping, kind: DocumentKind | None) -> Mapping:
    """The document with its ``schema_version`` (unchanged when it has no kind)."""

    if kind is None or kind in SELF_VERSIONED:
        return document
    return {**document, "schema_version": DOCUMENT_VERSIONS[kind]}


_NO_RESULT_KINDS = frozenset(
    {DocumentKind.CONNECTION, DocumentKind.OBSERVATION, DocumentKind.ERROR}
)


def stamp_document(
    args: argparse.Namespace, document: Mapping, *, error: bool
) -> Mapping:
    """The document as printed: stamped, with a followed result stamped inside it.

    A followed command that stops early prints the last confirmed document of
    the command's own kind in ``result``; ``result_kind`` names that kind so
    the wrapper is a tagged union. A wrapper around a document that has no
    published kind is itself unpublished.
    """

    kind = document_kind(args, document, error=error)
    if kind not in (DocumentKind.OBSERVATION, DocumentKind.ERROR):
        return stamp(document, kind)
    if _Word.RESULT not in document:
        return stamp(document, kind)
    inner = document[_Word.RESULT]
    inner_kind = document_kind(args, inner, error=False)
    if inner_kind is None or inner_kind in _NO_RESULT_KINDS:
        return document
    return stamp(
        {
            **document,
            "result_kind": inner_kind,
            "result": stamp(inner, inner_kind) if inner else inner,
        },
        kind,
    )
