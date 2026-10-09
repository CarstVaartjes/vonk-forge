"""Catalog reason codes."""

from ..wire_model import WireEnum


class CatalogCode(WireEnum):
    """Refusals of the recipe catalog and the recipe library documents."""

    ACTOR = "catalog.actor"
    CANDIDATE_EXISTS = "catalog.candidate_exists"
    CONFLICT = "catalog.conflict"
    DOCUMENT_EXISTS = "catalog.document_exists"
    DOCUMENT_INVALID = "catalog.document_invalid"
    DOCUMENT_MISSING = "catalog.document_missing"
    HEAD_MISSING = "catalog.head_missing"
    IDENTITIES = "catalog.identities"
    IDENTITY_CHANGED = "catalog.identity_changed"
    INSUFFICIENT_ROLE = "catalog.insufficient_role"
    INVALID_REQUEST = "catalog.invalid_request"
    MODEL_ARTIFACT_MISSING = "catalog.model_artifact_missing"
    MODEL_REFERENCE_INVALID = "catalog.model_reference_invalid"
    MODEL_REFERENCE_MISSING = "catalog.model_reference_missing"
    NOT_CANDIDATE = "catalog.not_candidate"
    NOT_FOUND = "catalog.not_found"
    RECIPE_INVALID = "catalog.recipe_invalid"
    REFERENCE = "catalog.reference"
    REFERENCE_MISSING = "catalog.reference_missing"
    REQUEST_FAILED = "catalog.request_failed"
    REVISION_MISSING = "catalog.revision_missing"
    STALE_REVISION = "catalog.stale_revision"
    UNAVAILABLE = "catalog.unavailable"
    DOCUMENT_INVALID_ = "recipe_library.document_invalid"
    HASH_MISMATCH = "recipe_library.hash_mismatch"
    MODEL_DOCUMENT_INVALID = "recipe_library.model_document_invalid"
    PACKAGE_HANDLE_INVALID = "recipe_library.package_handle_invalid"
    RELEASE_INVALID = "recipe_library.release_invalid"
    SOURCE_INVALID = "recipe_library.source_invalid"
    SIGNATURE_INVALID = "recipe_release.signature_invalid"


class CatalogSyncCode(WireEnum):
    """Catalog synchronisation refusals and per-item problems."""

    ACTOR_INVALID = "catalog.sync_actor_invalid"
    COMMIT_INVALID = "catalog.sync_commit_invalid"
    FAILED = "catalog.sync_failed"
    IDENTITY_CHANGED = "catalog.sync_identity_changed"
    IN_PROGRESS = "catalog.sync_in_progress"
    ITEM_FAILED = "catalog.sync_item_failed"
    LEASE_EXPIRED = "catalog.sync_lease_expired"
    MODEL_FAILED = "catalog.sync_model_failed"
    NOT_FOUND = "catalog.sync_not_found"
    PREBUILT_IMAGES_FAILED = "catalog.sync_prebuilt_images_failed"
    PREVIEW_CHANGED = "catalog.sync_preview_changed"
    REPOSITORY_CHANGED = "catalog.sync_repository_changed"
    REQUEST_INVALID = "catalog.sync_request_invalid"
    REQUEST_REUSED = "catalog.sync_request_reused"
    RESULT_UNREADABLE = "catalog.sync_result_unreadable"
    REVISION_CHANGED = "catalog.sync_revision_changed"
    STATE_INVALID = "catalog.sync_state_invalid"
    TRIGGER_INVALID = "catalog.sync_trigger_invalid"
    RECIPE_TOPOLOGY_CHANGED = "recipe.topology_changed"


class LibraryAssessmentCode(WireEnum):
    """Why a library entry is not assessed runnable on the current fleet."""

    ASSESSMENT_UNAVAILABLE = "library.assessment_unavailable"
    CACHE_MISSING = "library.cache_missing"
    CAPACITY_UNAVAILABLE = "library.capacity_unavailable"
    INSUFFICIENT_NODES = "library.insufficient_nodes"


class LibraryProjectionCode(WireEnum):
    """Reasons the library projection truncates what it lists."""

    EVIDENCE_TRUNCATED = "projection.evidence_truncated"
    REASONS_TRUNCATED = "projection.reasons_truncated"
