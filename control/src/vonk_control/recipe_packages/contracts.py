"""Release ingress models and categorized recipe-package outcomes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, TypeAdapter
from vonk_agent_protocol import (
    InvalidRequestError,
    InvalidRequestReason,
    RecipePackageCode,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
)
from vonk_forge_contracts import CONTRACT_MAJOR, document_sha256

from ..recipe_library_types import RecipeLibraryError, RecipeLibrarySnapshot

_RELEASE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class _ReleaseAsset(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    name: str
    state: str
    # GitHub reports ``sha256:<hex>`` for uploaded assets; it lets an
    # unchanged library bundle be recognised without downloading it.
    digest: str | None = None


class _ReleaseResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    tag_name: str
    draft: bool
    assets: list[_ReleaseAsset]


_RELEASE_LIST = TypeAdapter(list[_ReleaseResponse])


def _select_release(
    releases: list[_ReleaseResponse], selector: str
) -> _ReleaseResponse | None:
    """Pick the newest published release within this Controller's contract major."""

    best: tuple[tuple[int, int], _ReleaseResponse] | None = None
    for release in releases:
        tag = _RELEASE_TAG.fullmatch(release.tag_name)
        if release.draft or tag is None or int(tag[1]) != CONTRACT_MAJOR:
            continue
        if selector != "latest" and release.tag_name != selector:
            continue
        key = (int(tag[2]), int(tag[3]))
        if best is None or key > best[0]:
            best = (key, release)
    return None if best is None else best[1]


@dataclass(frozen=True, slots=True)
class _VerifiedRelease:
    """A release whose SHA256SUMS verified against the pinned publisher."""

    tag: str
    commit: str
    assets: frozenset[str]
    checksums: Mapping[str, str]
    checksums_raw: bytes
    bundle_raw: bytes


class RecipePackageError(RecipeLibraryError):
    """The trusted package descriptor or package contents are invalid."""


class RecipePackageUnsettled(UnknownOutcomeError, RecipePackageError):
    """Unavailable package bookkeeping, preserving the last verified generation."""

    def __init__(self, code: RecipePackageCode, detail: str) -> None:
        RecipePackageError.__init__(self, code, detail)
        self.typed_reason = WaitReason.OBSERVATION_UNAVAILABLE


class RecipePackageRequestInvalid(InvalidRequestError, RecipePackageError):
    """A caller-selected package is outside the verified snapshot, before effects."""

    def __init__(self, code: RecipePackageCode, detail: str) -> None:
        RecipePackageError.__init__(self, code, detail)
        self.typed_reason = InvalidRequestReason.MALFORMED
        self.typed_field = "snapshot"


def _snapshot_content(value: RecipeLibrarySnapshot) -> bytes:
    return canonical_message(
        (
            sorted(
                [
                    (
                        item.uri,
                        item.content_sha256,
                        item.package_sha256,
                        item.source_bundle_sha256,
                        item.prebuilt_image,
                    )
                    for item in value.items
                ],
                key=lambda item: item[0],
            ),
            sorted(document_sha256(document) for document in value.catalog_entities),
        )
    )
