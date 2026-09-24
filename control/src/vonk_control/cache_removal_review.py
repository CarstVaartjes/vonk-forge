"""Canonical review contract shared by cache-removal owners and clients.

The review is a read-only projection of one owner decision. Its digest binds
the target and every reported effect while deliberately excluding observation
time, so a fresh read with unchanged effects retains the same review identity.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator
from vonk_agent_protocol import canonical_message

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from .strict_json import StrictJSONModel

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ArtifactKind = Literal["model-set", "model-object", "runtime-image"]
AssetAvailability = Literal["verified", "partial", "missing", "unknown"]
AssetDisposition = Literal["remove", "retain-shared"]
FindingClassification = Literal["saved-reference", "active-work"]
MAX_CACHE_REMOVAL_REVIEW_BYTES = MAX_CONTROL_DOCUMENT_BYTES


class _CacheRemovalModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class CacheRemovalAsset(_CacheRemovalModel):
    """One exact cache identity and its owner-reported storage condition."""

    kind: ArtifactKind
    sha256: Digest
    expected_bytes: int | None = Field(default=None, ge=0)
    availability: AssetAvailability
    available_bytes: int | None = Field(default=None, ge=0)
    disposition: AssetDisposition

    @model_validator(mode="after")
    def byte_observation_is_consistent(self) -> CacheRemovalAsset:
        if self.availability == "verified" and (
            self.expected_bytes is None or self.available_bytes != self.expected_bytes
        ):
            raise ValueError("verified asset requires its exact expected byte length")
        return self


class CacheRemovalFinding(_CacheRemovalModel):
    """One existing owner predicate's exact reference or active-work finding."""

    classification: FindingClassification
    asset_kind: ArtifactKind
    asset_sha256: Digest
    owner_kind: str = Field(min_length=1, max_length=96)
    owner_id: str = Field(min_length=1, max_length=256)
    state: str = Field(min_length=1, max_length=64)
    detail: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=512)


class CacheRemovalBlocker(_CacheRemovalModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,95}$")
    detail: str = Field(min_length=1, max_length=512)
    retryable: bool
    recovery_actions: list[str] = Field(default_factory=list)


class CacheRemovalReviewContent(_CacheRemovalModel):
    """Digest input plus observation time, before the digest is attached."""

    schema_version: Literal[2] = 2
    action: Literal["remove"] = "remove"
    resource_kind: Literal["model", "recipe"]
    selector: str = Field(min_length=1, max_length=256)
    # Model reviews bind the exact model content digest; recipe reviews bind
    # the exact CatalogDocumentRevision.id. The resource_kind disambiguates it.
    target_identity: str = Field(min_length=1, max_length=128)
    # Null for model removal; recipe removal binds the operator's exact choice.
    with_model: bool | None
    assets: list[CacheRemovalAsset]
    references: list[CacheRemovalFinding]
    active_work: list[CacheRemovalFinding]
    blockers: list[CacheRemovalBlocker]
    observed_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def resource_fields_match(self) -> CacheRemovalReviewContent:
        if self.resource_kind == "model":
            if self.with_model is not None:
                raise ValueError("model removal has no with_model choice")
            if len(self.target_identity) != 64 or any(
                character not in "0123456789abcdef"
                for character in self.target_identity
            ):
                raise ValueError("model target identity must be a content digest")
        elif self.with_model is None:
            raise ValueError("recipe removal requires an explicit model choice")
        if any(item.classification != "saved-reference" for item in self.references):
            raise ValueError("references must contain saved-reference findings")
        if any(item.classification != "active-work" for item in self.active_work):
            raise ValueError("active_work must contain active-work findings")
        identities = [(item.kind, item.sha256) for item in self.assets]
        if len(identities) != len(set(identities)):
            raise ValueError("cache-removal asset identities must be unique")
        _require_review_byte_budget(self.model_dump(mode="json", exclude_none=False))
        return self


class CacheRemovalReview(CacheRemovalReviewContent):
    """A complete review whose digest is verified during model validation."""

    review_digest: Digest

    @model_validator(mode="after")
    def digest_matches_content(self) -> CacheRemovalReview:
        expected = cache_removal_review_digest(self)
        if self.review_digest != expected:
            raise ValueError("cache-removal review digest does not match its content")
        return self


def _asset_order(item: CacheRemovalAsset) -> tuple[str, str]:
    return item.kind, item.sha256


def _finding_order(item: CacheRemovalFinding) -> tuple[str, ...]:
    return (
        item.asset_kind,
        item.asset_sha256,
        item.owner_kind,
        item.owner_id,
        item.state,
        item.detail,
        item.reason,
    )


def _blocker_order(item: CacheRemovalBlocker) -> tuple[str, str]:
    return item.code, item.detail


def _require_review_byte_budget(document: object) -> None:
    """Bound the complete canonical document accepted by ControlClient."""

    observed = len(canonical_message(document))
    if observed > MAX_CACHE_REMOVAL_REVIEW_BYTES:
        raise ValueError(
            "cache-removal review exceeds its "
            f"{MAX_CACHE_REMOVAL_REVIEW_BYTES}-byte limit "
            f"({observed} bytes observed)"
        )


def _canonical_content(content: CacheRemovalReviewContent) -> dict[str, object]:
    normalized = content.model_copy(
        update={
            "assets": sorted(content.assets, key=_asset_order),
            "references": sorted(content.references, key=_finding_order),
            "active_work": sorted(content.active_work, key=_finding_order),
            "blockers": sorted(content.blockers, key=_blocker_order),
        }
    )
    document = normalized.model_dump(mode="json", exclude_none=False)
    document.pop("observed_at")
    document.pop("review_digest", None)
    return document


def cache_removal_review_digest(
    content: CacheRemovalReviewContent | CacheRemovalReview,
) -> str:
    """Return the stable digest over reviewed identities and exact effects."""

    encoded = canonical_message(_canonical_content(content))
    return hashlib.sha256(encoded).hexdigest()


def seal_cache_removal_review(
    content: CacheRemovalReviewContent,
) -> CacheRemovalReview:
    """Sort owner findings and attach their deterministic review digest."""

    normalized = content.model_copy(
        update={
            "assets": sorted(content.assets, key=_asset_order),
            "references": sorted(content.references, key=_finding_order),
            "active_work": sorted(content.active_work, key=_finding_order),
            "blockers": sorted(content.blockers, key=_blocker_order),
        }
    )
    return CacheRemovalReview.model_validate(
        {
            **normalized.model_dump(mode="json", exclude_none=False),
            "review_digest": cache_removal_review_digest(normalized),
        }
    )


__all__ = [
    "MAX_CACHE_REMOVAL_REVIEW_BYTES",
    "ArtifactKind",
    "AssetAvailability",
    "AssetDisposition",
    "CacheRemovalAsset",
    "CacheRemovalBlocker",
    "CacheRemovalFinding",
    "CacheRemovalReview",
    "CacheRemovalReviewContent",
    "FindingClassification",
    "cache_removal_review_digest",
    "seal_cache_removal_review",
]
