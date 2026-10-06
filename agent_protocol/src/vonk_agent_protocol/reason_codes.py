"""Closed reason, blocker, warning and attention codes shared by Python, Rust and TypeScript.

Every code the Controller shows an operator as *why* something is waiting, refused,
blocked or degraded is a member of one of these enums, grouped by the domain that
raises it.  ``scripts/export-agent-wire-schema`` publishes them through
:class:`ReasonCodeVocabulary`, ``scripts/generate-agent-wire`` turns that into Rust
types and ``scripts/generate-control-clients`` into OpenAPI and TypeScript, so no
consumer spells a code by hand.  The vocabulary ratchet
(``control/tests/vocabulary_literals.py``) fails a string literal that equals a
member and a string constant in any code position of the Controller.

The values are the spellings already stored in rows and shown by the API: this
module adds no new word, it only closes the set.  The exceptions are the agent's
runtime preflight finding codes (:class:`RuntimePreflightFindingCode`), which an
agent used to write as bare free text and which now carry one domain prefix so they
cannot collide with another domain's word; the old spellings are adopted on read
(:func:`adopt_preflight_finding_code`).  A code is a ``StrEnum``, so it
compares, hashes and serialises as its word.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache

from .wire_model import WireEnum, WireModel


class AdmissionCode(WireEnum):
    """The shared admission lock refused because capacity is held by another admission."""

    CAPACITY_BUSY = "admission.capacity_busy"


class AgentEvidenceCode(WireEnum):
    """Optional agent evidence that was dropped so the mandatory report is kept.

    An agent report carries a mandatory core (identity, lease, capacity, outcome)
    and optional evidence (NICs, the NAS route, fabric details, readings,
    progress, diagnostics). Invalid optional evidence is never a reason to refuse
    the core: the evidence is dropped and one of these words names what was lost,
    on the agent that dropped it and on the Controller that received it.
    """

    CLAIM_HINT_DROPPED = "agent_evidence.claim_hint_dropped"
    FAILURE_DIAGNOSTICS_DROPPED = "agent_evidence.failure_diagnostics_dropped"
    INVENTORY_FABRIC_DROPPED = "agent_evidence.inventory_fabric_dropped"
    INVENTORY_NAS_ROUTE_DROPPED = "agent_evidence.inventory_nas_route_dropped"
    INVENTORY_NETWORK_DROPPED = "agent_evidence.inventory_network_dropped"
    INVENTORY_NETWORK_INTERFACE_DROPPED = (
        "agent_evidence.inventory_network_interface_dropped"
    )
    PROGRESS_DROPPED = "agent_evidence.progress_dropped"
    TELEMETRY_READING_DROPPED = "agent_evidence.telemetry_reading_dropped"


class ArtifactLifecycleCode(WireEnum):
    """Why an artifact (model file, image archive, blob) cannot be removed, referenced or changed right now."""

    ASSET_AVAILABILITY_UNKNOWN = "artifact.asset_availability_unknown"
    DELETION_BUSY = "artifact.deletion_busy"
    DELETION_FENCE_LOST = "artifact.deletion_fence_lost"
    DELETION_IN_PROGRESS = "artifact.deletion_in_progress"
    REFERENCE_BUSY = "artifact.reference_busy"
    REFERENCE_CHANGED = "artifact.reference_changed"
    REFERENCE_IDENTITY_MISMATCH = "artifact.reference_identity_mismatch"
    REFERENCE_SCAN_FAILED = "artifact.reference_scan_failed"
    REFERENCE_SCAN_LIMITED = "artifact.reference_scan_limited"
    REFERENCE_TIMEOUT = "artifact.reference_timeout"
    REFERENCE_UNAVAILABLE = "artifact.reference_unavailable"
    REMOVAL_OWNER_INVALID = "artifact.removal_owner_invalid"
    REMOVAL_OWNER_UNRESOLVED = "artifact.removal_owner_unresolved"


class CacheReferenceReason(WireEnum):
    """What keeps a cached artifact from being removed."""

    RECIPE_INSTALLATION = "recipe-installation"
    RUNNING_MODEL = "running-model"
    SAVED_PROFILE = "saved-profile"


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


class ClusterMappingCode(WireEnum):
    """Refusals of a cluster mapping (recipe-to-Spark assignment) request."""

    ACTOR = "mapping.actor"
    ENDPOINT_OWNER = "mapping.endpoint_owner"
    NODE_COUNT = "mapping.node_count"
    NODE_INCOMPATIBLE = "mapping.node_incompatible"
    NODE_UNKNOWN = "mapping.node_unknown"
    NODES_INVALID = "mapping.nodes_invalid"
    OPTION_INVALID = "mapping.option_invalid"
    PARAMETER_TYPE = "mapping.parameter_type"
    PARAMETER_UNKNOWN = "mapping.parameter_unknown"
    PARAMETER_VALUE = "mapping.parameter_value"
    PARAMETERS_INVALID = "mapping.parameters_invalid"
    READY_IMMUTABLE = "mapping.ready_immutable"
    RECIPE_UNRESOLVED = "mapping.recipe_unresolved"
    STALE_PLAN = "mapping.stale_plan"
    TOPOLOGY_INVALID = "mapping.topology_invalid"


class ControllerErrorCode(WireEnum):
    """Generic Controller request and fleet-operation problem codes."""

    CONFLICT = "controller.conflict"
    FLEET_REVOCATION_UNCERTAIN = "controller.fleet.revocation_uncertain"
    FLEET_UPGRADE_CONFLICT = "controller.fleet.upgrade_conflict"
    HTTP = "controller.http_"
    INTERNAL_ERROR = "controller.internal_error"
    INVALID_REQUEST = "controller.invalid_request"
    NOT_FOUND = "controller.not_found"
    RATE_LIMITED = "controller.rate_limited"
    REQUEST_TOO_LARGE = "controller.request_too_large"
    TIMEOUT = "controller.timeout"
    UNAVAILABLE = "controller.unavailable"


class DistributionCode(WireEnum):
    """Why a distribution assignment object cannot be served to a Spark."""

    ASSIGNMENT_CONFLICT = "distribution.assignment_conflict"
    EXPIRED = "distribution.expired"
    MODEL_SET_IDENTITY_UNAVAILABLE = "distribution.model_set_identity_unavailable"
    MODEL_SET_MISMATCH = "distribution.model_set_mismatch"
    OBJECT_INVALID = "distribution.object_invalid"
    OBJECT_UNAVAILABLE = "distribution.object_unavailable"
    RUNTIME_IMAGE_MISMATCH = "distribution.runtime_image_mismatch"
    UNASSIGNED = "distribution.unassigned"
    WRONG_NODE = "distribution.wrong_node"


class HelperErrorCode(WireEnum):
    """Every code the privileged helper, or the agent speaking about it, names as an error.

    The helper builds its rejection from a member; the agent reads a reply code
    through the enum, so a word outside it is a malformed rejection, never a code
    the agent invents or forwards.  ``runtime_helper_*`` is the spelling of an
    agent-side cause in failure evidence.
    """

    CALL_JOIN_FAILED = "call_join_failed"
    CONCURRENCY_LIMIT = "concurrency_limit"
    GRANT_INVALID = "grant_invalid"
    GRANT_NODE_MISMATCH = "grant_node_mismatch"
    GRANT_UNAUTHORIZED = "grant_unauthorized"
    INSPECTION_OUTCOME_INVALID = "inspection_outcome_invalid"
    INSTALLATION_RECONCILIATION_BUSY = "installation_reconciliation_busy"
    INSTALLATION_RECONCILIATION_STORAGE_UNAVAILABLE = (
        "installation_reconciliation_storage_unavailable"
    )
    MESSAGE_FRAMING_INVALID = "message_framing_invalid"
    OPERATION_COMMAND_FAILED = "operation_command_failed"
    OPERATION_FAILED = "operation_failed"
    OPERATION_INVALID = "operation_invalid"
    OPERATION_INVALID_ARTIFACT = "operation_invalid_artifact"
    OPERATION_IO = "operation_io"
    OPERATION_STOP_UNCERTAIN = "operation_stop_uncertain"
    OPERATION_UNSAFE_PATH = "operation_unsafe_path"
    OUTCOME_MALFORMED = "outcome_malformed"
    PACKAGE_CUSTODY_FAILED = "package_custody_failed"
    PACKAGE_INSTALL_FAILED = "package_install_failed"
    PACKAGE_METADATA_FAILED = "package_metadata_failed"
    PACKAGE_PREFLIGHT_FAILED = "package_preflight_failed"
    PACKAGE_VERIFICATION_FAILED = "package_verification_failed"
    PEER_IDENTITY_INVALID = "peer_identity_invalid"
    REJECTION_MALFORMED = "rejection_malformed"
    REQUEST_ARGUMENTS_PRESENCE_INVALID = "request_arguments_presence_invalid"
    REQUEST_ARGUMENT_NUL_BYTE = "request_argument_nul_byte"
    REQUEST_ATTEMPT_INVALID = "request_attempt_invalid"
    REQUEST_BYTES_INVALID = "request_bytes_invalid"
    REQUEST_DOCUMENT_INVALID = "request_document_invalid"
    REQUEST_ENCODING_INVALID = "request_encoding_invalid"
    REQUEST_INSTALLATION_IDENTITY_INVALID = "request_installation_identity_invalid"
    REQUEST_INVALID = "request_invalid"
    REQUEST_LEDGER_FAILED = "request_ledger_failed"
    REQUEST_PLAN_BINDING_INVALID = "request_plan_binding_invalid"
    REQUEST_PLAN_BYTES_INVALID = "request_plan_bytes_invalid"
    REQUEST_REPLAYED = "request_replayed"
    REQUEST_SCHEMA_VERSION_INVALID = "request_schema_version_invalid"
    REQUEST_STORAGE_INVALID = "request_storage_invalid"
    RESPONSE_UNBOUND = "response_unbound"
    RUNTIME_AUTHORITY_UNAVAILABLE = "runtime_authority_unavailable"
    RUNTIME_ENDPOINT_FIREWALL_REJECTED = "runtime_endpoint_firewall_rejected"
    RUNTIME_FABRIC_FIREWALL_REJECTED = "runtime_fabric_firewall_rejected"
    RUNTIME_FABRIC_UNAVAILABLE = "runtime_fabric_unavailable"
    RUNTIME_HELPER_CALL_JOIN_FAILED = "runtime_helper_call_join_failed"
    RUNTIME_HELPER_INSPECTION_OUTCOME_INVALID = (
        "runtime_helper_inspection_outcome_invalid"
    )
    RUNTIME_HELPER_MESSAGE_FRAMING_INVALID = "runtime_helper_message_framing_invalid"
    RUNTIME_HELPER_OUTCOME_MALFORMED = "runtime_helper_outcome_malformed"
    RUNTIME_HELPER_PROTOCOL_INVALID = "runtime_helper_protocol_invalid"
    RUNTIME_HELPER_REJECTION_MALFORMED = "runtime_helper_rejection_malformed"
    RUNTIME_HELPER_REQUEST_ARGUMENTS_PRESENCE_INVALID = (
        "runtime_helper_request_arguments_presence_invalid"
    )
    RUNTIME_HELPER_REQUEST_ARGUMENT_NUL_BYTE = (
        "runtime_helper_request_argument_nul_byte"
    )
    RUNTIME_HELPER_REQUEST_ATTEMPT_INVALID = "runtime_helper_request_attempt_invalid"
    RUNTIME_HELPER_REQUEST_BYTES_INVALID = "runtime_helper_request_bytes_invalid"
    RUNTIME_HELPER_REQUEST_DOCUMENT_INVALID = "runtime_helper_request_document_invalid"
    RUNTIME_HELPER_REQUEST_ENCODING_INVALID = "runtime_helper_request_encoding_invalid"
    RUNTIME_HELPER_REQUEST_INSTALLATION_IDENTITY_INVALID = (
        "runtime_helper_request_installation_identity_invalid"
    )
    RUNTIME_HELPER_REQUEST_PLAN_BINDING_INVALID = (
        "runtime_helper_request_plan_binding_invalid"
    )
    RUNTIME_HELPER_REQUEST_PLAN_BYTES_INVALID = (
        "runtime_helper_request_plan_bytes_invalid"
    )
    RUNTIME_HELPER_REQUEST_SCHEMA_VERSION_INVALID = (
        "runtime_helper_request_schema_version_invalid"
    )
    RUNTIME_HELPER_REQUEST_STORAGE_INVALID = "runtime_helper_request_storage_invalid"
    RUNTIME_HELPER_RESPONSE_UNBOUND = "runtime_helper_response_unbound"
    RUNTIME_HELPER_STOP_UNCERTAIN = "runtime_helper_stop_uncertain"
    RUNTIME_HELPER_SYSTEM_CLOCK_INVALID = "runtime_helper_system_clock_invalid"
    RUNTIME_HELPER_UNAVAILABLE = "runtime_helper_unavailable"
    RUNTIME_IMAGE_IDENTITY_INVALID = "runtime_image_identity_invalid"
    RUNTIME_IMAGE_INSPECT_FAILED = "runtime_image_inspect_failed"
    RUNTIME_IMAGE_LOAD_FAILED = "runtime_image_load_failed"
    RUNTIME_IMAGE_RECEIPT_FAILED = "runtime_image_receipt_failed"
    RUNTIME_PROCESS_EXITED = "runtime_process_exited"
    RUNTIME_RUN_MISSING = "runtime_run_missing"
    SYSTEM_CLOCK_INVALID = "system_clock_invalid"


class ImageStoreCode(WireEnum):
    """Refusals and damage found by the Controller OCI image store."""

    BUSY = "image_store.busy"
    COLLECTION_DEFERRED = "image_store.collection_deferred"
    COPY_FAILED = "image_store.copy_failed"
    DAMAGED_RECEIPT_EVICTED = "image_store.damaged_receipt_evicted"
    DIGEST_INVALID = "image_store.digest_invalid"
    IMPORT_INCOMPLETE = "image_store.import_incomplete"
    MANIFEST_CORRUPT = "image_store.manifest_corrupt"
    MANIFEST_INVALID = "image_store.manifest_invalid"
    MANIFEST_UNREADABLE = "image_store.manifest_unreadable"
    MANIFEST_UNSUPPORTED = "image_store.manifest_unsupported"
    REFERENCE_SCAN_FAILED = "image_store.reference_scan_failed"
    REFERENCE_UNPINNED = "image_store.reference_unpinned"
    REFERENCED_MANIFEST_DAMAGED = "image_store.referenced_manifest_damaged"


class InstallAdmissionCode(WireEnum):
    """Why an installation is not admitted (or is waiting) on a Spark."""

    AGENT_UPGRADE_REQUIRED = "install.agent_upgrade_required"
    ARTIFACT_SIZE_UNDERDECLARED = "install.artifact_size_underdeclared"
    ARTIFACT_STORE_READ_ONLY = "install.artifact_store_read_only"
    CAPACITY_BUSY = "install.capacity_busy"
    COMPILED_PLAN_UNAVAILABLE = "install.compiled_plan_unavailable"
    DEPENDENCIES_STALE = "install.dependencies_stale"
    IMAGE_DISTRIBUTION_PENDING = "install.image_distribution_pending"
    IMAGE_SIZE_UNDERDECLARED = "install.image_size_underdeclared"
    INSUFFICIENT_DISK = "install.insufficient_disk"
    INVENTORY_MISSING = "install.inventory_missing"
    MODEL_IDENTITY_UNAVAILABLE = "install.model_identity_unavailable"
    PLAN_INVALID = "install.plan_invalid"
    PLAN_STALE = "install.plan_stale"
    STALE_INVENTORY = "install.stale_inventory"


class InstallDegradedReason(WireEnum):
    """Why an installation is shown partial in the fleet projection."""

    EXTERNAL_MEMBER = "external-member"
    MAPPING_INCOMPLETE = "mapping-incomplete"
    MISSING_RANKS = "missing-ranks"
    UNEXPECTED_RANKS = "unexpected-ranks"
    RANK_MEMBERSHIP_MISMATCH = "rank-membership-mismatch"
    INSTALLATION_NOT_INSTALLED = "installation-not-installed"
    RANK_NOT_INSTALLED = "rank-not-installed"
    RANK_INCOMPLETE_BYTES = "rank-incomplete-bytes"


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


class ModelCacheBlockerCode(WireEnum):
    """Why a model download plan is blocked or waiting."""

    INSUFFICIENT_RESERVED_STORAGE = "insufficient-reserved-storage"
    MODEL_NOT_CACHED = "model-not-cached"
    RECIPE_NOT_CACHED = "recipe-not-cached"


class ModelCacheCode(WireEnum):
    """Model cache resolution, storage, removal and operation problems."""

    ACCESS_RECHECK_UNAVAILABLE = "model_cache.access_recheck_unavailable"
    ARTIFACT_COUNT = "model_cache.artifact_count"
    ARTIFACT_DUPLICATE = "model_cache.artifact_duplicate"
    ARTIFACT_INVALID = "model_cache.artifact_invalid"
    ARTIFACT_MISSING = "model_cache.artifact_missing"
    ARTIFACT_UNVERIFIED = "model_cache.artifact_unverified"
    CANCELLATION_INVALID = "model_cache.cancellation_invalid"
    CANCELLATION_KEY_REUSED = "model_cache.cancellation_key_reused"
    CAPACITY = "model_cache.capacity"
    COVERAGE_INCOMPLETE = "model_cache.coverage_incomplete"
    CREDENTIALS_MISSING = "model_cache.credentials_missing"
    CURSOR_INVALID = "model_cache.cursor_invalid"
    DEPENDENCY_COUNT = "model_cache.dependency_count"
    DIGEST_INVALID = "model_cache.digest_invalid"
    DIGEST_MISMATCH = "model_cache.digest_mismatch"
    DIGEST_SIZE_CONFLICT = "model_cache.digest_size_conflict"
    DOCUMENT_UNREADABLE = "model_cache.document_unreadable"
    DOWNLOAD_BLOCKED = "model_cache.download_blocked"
    ENTRY_MISSING = "model_cache.entry_missing"
    FIXTURE_SOURCES_FORBIDDEN = "model_cache.fixture_sources_forbidden"
    IDENTITY_CONFLICT = "model_cache.identity_conflict"
    IDENTITY_MISMATCH = "model_cache.identity_mismatch"
    INTERRUPTED = "model_cache.interrupted"
    LOCK_UNAVAILABLE = "model_cache.lock_unavailable"
    MANIFEST_IDENTITY_MISMATCH = "model_cache.manifest_identity_mismatch"
    MANIFEST_INVALID = "model_cache.manifest_invalid"
    MANIFEST_TOO_LARGE = "model_cache.manifest_too_large"
    MODEL_CONTENT_DIGESTS_INVALID = "model_cache.model_content_digests_invalid"
    MODEL_DEFINITION_INVALID = "model_cache.model_definition_invalid"
    MODEL_DEFINITION_MISSING = "model_cache.model_definition_missing"
    MODEL_DEPENDENCY_CYCLE = "model_cache.model_dependency_cycle"
    MODEL_PIN_INVALID = "model_cache.model_pin_invalid"
    MODEL_VARIANT_INVALID = "model_cache.model_variant_invalid"
    NOT_CANCELLABLE = "model_cache.not_cancellable"
    OBJECT_BUSY = "model_cache.object_busy"
    OPERATION_FAILED = "model_cache.operation_failed"
    OPERATION_MISSING = "model_cache.operation_missing"
    OPERATION_NOT_OBSERVABLE = "model_cache.operation_not_observable"
    OPERATION_NOT_RETRYABLE = "model_cache.operation_not_retryable"
    OPERATION_UNREADABLE = "model_cache.operation_unreadable"
    PAYLOAD_INVALID = "model_cache.payload_invalid"
    PIN_MISMATCH = "model_cache.pin_mismatch"
    PIN_REQUIRED = "model_cache.pin_required"
    PLAN_INVALID = "model_cache.plan_invalid"
    RANGE_INVALID = "model_cache.range_invalid"
    RATE_LIMITED = "model_cache.rate_limited"
    RECIPE_IDENTITY_AMBIGUOUS = "model_cache.recipe_identity_ambiguous"
    RECIPE_IDENTITY_INVALID = "model_cache.recipe_identity_invalid"
    RECIPE_IDENTITY_MISSING = "model_cache.recipe_identity_missing"
    RECIPE_INVALID = "model_cache.recipe_invalid"
    RECIPE_MODEL_MISSING = "model_cache.recipe_model_missing"
    RECIPE_REVISION_INVALID = "model_cache.recipe_revision_invalid"
    RECIPE_REVISION_MISSING = "model_cache.recipe_revision_missing"
    REDIRECT_FORBIDDEN = "model_cache.redirect_forbidden"
    RELEASE_ASSET_IDENTITY_CONFLICT = "model_cache.release_asset_identity_conflict"
    RELEASE_METADATA_INVALID = "model_cache.release_metadata_invalid"
    REMOVAL_CHILD_FAILED = "model_cache.removal_child_failed"
    REMOVAL_CHILD_INVALID = "model_cache.removal_child_invalid"
    REMOVAL_CHILD_MISMATCH = "model_cache.removal_child_mismatch"
    REMOVAL_CHILD_MISSING = "model_cache.removal_child_missing"
    REMOVAL_CHILD_PENDING = "model_cache.removal_child_pending"
    REMOVAL_INVALID = "model_cache.removal_invalid"
    REMOVAL_PATH_UNSAFE = "model_cache.removal_path_unsafe"
    REMOVAL_PLAN_INVALID = "model_cache.removal_plan_invalid"
    REMOVAL_REFERENCED = "model_cache.removal_referenced"
    REMOVAL_SCOPE_CHANGED = "model_cache.removal_scope_changed"
    REMOVAL_SCOPE_INVALID = "model_cache.removal_scope_invalid"
    REMOVAL_SCOPE_UNAVAILABLE = "model_cache.removal_scope_unavailable"
    REMOVAL_WAIT = "model_cache.removal_wait"
    REQUEST_KEY_INVALID = "model_cache.request_key_invalid"
    REQUEST_KEY_REUSED = "model_cache.request_key_reused"
    REVIEW_INVALID = "model_cache.review_invalid"
    REVIEW_UNAVAILABLE = "model_cache.review_unavailable"
    REVISION_INVALID = "model_cache.revision_invalid"
    REVISION_MISSING = "model_cache.revision_missing"
    SCHEMA_UNSUPPORTED = "model_cache.schema_unsupported"
    SELECTOR_AMBIGUOUS = "model_cache.selector_ambiguous"
    SELECTOR_INVALID = "model_cache.selector_invalid"
    SELECTOR_MISSING = "model_cache.selector_missing"
    SOURCE_INVALID = "model_cache.source_invalid"
    SOURCE_SIZE_MISMATCH = "model_cache.source_size_mismatch"
    SOURCE_TRUNCATED = "model_cache.source_truncated"
    SOURCE_UNAVAILABLE = "model_cache.source_unavailable"
    SOURCE_UNSUPPORTED = "model_cache.source_unsupported"
    SOURCE_UNTRUSTED = "model_cache.source_untrusted"
    STALE_PLAN = "model_cache.stale_plan"
    UNAVAILABLE = "model_cache.unavailable"
    UPSTREAM_CHECK_BUDGET_EXHAUSTED = "model_cache.upstream_check_budget_exhausted"
    UPSTREAM_CHECK_FAILED = "model_cache.upstream_check_failed"
    UPSTREAM_REVISION_INVALID = "model_cache.upstream_revision_invalid"


class NodeOfflineReason(WireEnum):
    """Why a node is shown offline in the fleet projection."""

    UNREGISTERED = "unregistered"
    AGENT_INACTIVE = "agent-inactive"
    AGENT_REVOKED = "agent-revoked"
    NEVER_SEEN = "never-seen"
    LAST_SEEN_IN_FUTURE = "last-seen-in-future"
    STALE = "stale"
    CERTIFICATE_MISSING = "certificate-missing"
    CERTIFICATE_NOT_YET_VALID = "certificate-not-yet-valid"
    CERTIFICATE_EXPIRED = "certificate-expired"
    CERTIFICATE_REVOKED = "certificate-revoked"
    CERTIFICATE_INACTIVE = "certificate-inactive"


class OperationFailureCode(WireEnum):
    """Error codes of a stored operation failure evidence record."""

    FLEET_PROFILE_APPLICATION_FAILED = "fleet_profile_application_failed"
    ARTIFACT_PROCESS_FAILED = "artifact_process_failed"


class PrebuiltImageCode(WireEnum):
    """Why a prebuilt runtime image is, or is not, used."""

    BUILD_KEY_MISMATCH = "prebuilt.build_key_mismatch"
    NOT_PINNED = "prebuilt.not_pinned"
    PULL_FAILED_RECENTLY = "prebuilt.pull_failed_recently"
    USED = "prebuilt.used"


class ProfileReasonCode(WireEnum):
    """Reasons a fleet profile cannot be applied or is waiting."""

    ADMISSION_BUSY = "profile.admission_busy"
    ADMISSION_EFFECT_BUSY = "profile.admission_effect_busy"
    APPLICATION_INTENT_INVALID = "profile.application_intent.invalid"
    CHOICES_UNREADABLE = "profile.choices_unreadable"
    CLEANUP_DELEGATED = "profile.cleanup_delegated"
    DISTRIBUTED_CROSS_SCOPE = "profile.distributed_cross_scope"
    FAILURE_REPEATED = "profile.failure_repeated"
    INCOMPLETE_MULTI_SPARK_MODEL = "profile.incomplete_multi_spark_model"
    INTERRUPTION_EXPECTED = "profile.interruption_expected"
    PENDING_CROSS_SCOPE = "profile.pending_cross_scope"
    PREPARATION_NOT_STARTED = "profile.preparation_not_started"
    PREPARATION_SCOPE_MISMATCH = "profile.preparation_scope_mismatch"
    PREPARATION_UNAVAILABLE = "profile.preparation_unavailable"
    RECIPE_UNAVAILABLE = "profile.recipe_unavailable"
    RECOVERY_ASSIGNMENTS_CHANGED = "profile.recovery_assignments_changed"
    RECOVERY_CACHE_PENDING = "profile.recovery_cache_pending"
    RECOVERY_SCOPE_CHANGED = "profile.recovery_scope_changed"
    RECOVERY_WAITING = "profile.recovery_waiting"
    RESOURCE_RECHECK_UNAVAILABLE = "profile.resource_recheck_unavailable"
    RETRY_CONFLICT = "profile.retry_conflict"
    RETRY_EXECUTOR_UNAVAILABLE = "profile.retry_executor_unavailable"
    RETRY_INTENT_UNAVAILABLE = "profile.retry_intent_unavailable"
    RETRY_REVIEW_UNAVAILABLE = "profile.retry_review_unavailable"
    REVIEW_STALE = "profile.review_stale"
    RUNTIME_IMAGE_REBUILD_PENDING = "profile.runtime_image_rebuild_pending"
    SHARED_INSTALLATION_SCOPE = "profile.shared_installation_scope"
    SPARK_REMOVED = "profile.spark_removed"
    SPARK_UNAVAILABLE = "profile.spark_unavailable"
    STALE_PLAN = "profile.stale_plan"
    SWITCH_AUTHORITY_UNAVAILABLE = "profile.switch_authority_unavailable"
    SWITCH_SCOPE_UNRESOLVED = "profile.switch_scope_unresolved"
    TOPOLOGY_INCOMPLETE = "profile.topology_incomplete"
    RECOVERY_ARTIFACT_CHANGED = "profile.recovery_artifact_changed"
    RUNTIME_IMAGE_CHANGED = "profile.runtime-image-changed"
    SELECTION_LOST = "profile.selection_lost"
    ASSET_RESERVATION_UNAVAILABLE = "profile.asset_reservation_unavailable"


class ProjectionCode(WireEnum):
    """Warnings and attention items of the fleet and library projections."""

    CPU_LOW_CLOCK = "cpu.low-clock"
    INSTALL_PARTIAL = "install.partial"
    INVENTORY_MISSING = "inventory.missing"
    INVENTORY_STALE = "inventory.stale"
    NETWORK_NAS_ROUTE_WIFI_NO_WIRED_PORT = "network.nas-route-wifi-no-wired-port"
    NETWORK_NAS_ROUTE_WIFI_WIRED_PORT_DOWN = "network.nas-route-wifi-wired-port-down"
    NETWORK_NAS_ROUTE_WIFI_WIRED_PORT_UNUSED = (
        "network.nas-route-wifi-wired-port-unused"
    )
    NODE_OFFLINE = "node.offline"
    PROFILE_RETRYING = "profile.retrying"
    RECIPE_UPDATE_AVAILABLE = "recipe.update_available"
    RUN_DEGRADED = "run.degraded"
    TELEMETRY_DELAYED = "telemetry.delayed"
    TELEMETRY_MISSING = "telemetry.missing"
    TELEMETRY_STALE = "telemetry.stale"


class RecipeBuildCode(WireEnum):
    """Recipe image build planning and recording problems."""

    ADAPTER_UNAVAILABLE = "build.adapter_unavailable"
    CANCELLATION_PENDING = "build.cancellation_pending"
    CAPABILITY_MISSING = "build.capability_missing"
    CAPACITY_BUSY = "build.capacity_busy"
    CAPACITY_CONTRACT_INVALID = "build.capacity_contract_invalid"
    CONSUMER_BUSY = "build.consumer_busy"
    CONSUMER_INVALID = "build.consumer_invalid"
    CONTRACT_INVALID = "build.contract_invalid"
    DEPENDENCIES_STALE = "build.dependencies_stale"
    EVIDENCE_INVALID = "build.evidence_invalid"
    IMAGE_SIZE_INVALID = "build.image_size_invalid"
    INPUT_MISMATCH = "build.input_mismatch"
    INSUFFICIENT_DISK = "build.insufficient_disk"
    INSUFFICIENT_MEMORY = "build.insufficient_memory"
    INVENTORY_MISSING = "build.inventory_missing"
    INVENTORY_STALE = "build.inventory_stale"
    NETWORK_CAPABILITY_MISSING = "build.network_capability_missing"
    NODE_INCOMPATIBLE = "build.node_incompatible"
    NODE_UNKNOWN = "build.node_unknown"
    PLAN_INVALID = "build.plan_invalid"
    PRODUCER_INVALID = "build.producer_invalid"
    RECIPE_UNRESOLVED = "build.recipe_unresolved"
    RESOLUTION_STALE = "build.resolution_stale"
    RESOURCES_INVALID = "build.resources_invalid"
    RESULT_CONFLICT = "build.result_conflict"
    RUNTIME_CHANGED = "build.runtime_changed"
    SECURITY_INVALID = "build.security_invalid"
    SOURCE_INVALID = "build.source_invalid"
    SOURCE_UNAVAILABLE = "build.source_unavailable"
    STATE = "build.state"
    SHARED_CONSUMERS = "build.shared_consumers"


class RecipeImageCode(WireEnum):
    """Runtime image availability, preparation and cache-removal problems."""

    ACTION_INVALID = "recipe_image.action_invalid"
    BUILD_CANCELLED = "recipe_image.build_cancelled"
    BUILD_CAPACITY_WAIT = "recipe_image.build_capacity_wait"
    BUILD_FAILED = "recipe_image.build_failed"
    BUILD_INPUT_MISSING = "recipe_image.build_input_missing"
    BUILD_INVALID = "recipe_image.build_invalid"
    BUILD_UNAVAILABLE = "recipe_image.build_unavailable"
    BUILD_WAIT = "recipe_image.build_wait"
    BUILDER_BUSY = "recipe_image.builder_busy"
    BUILDER_OCCUPIED = "recipe_image.builder_occupied"
    CANCEL_BUSY = "recipe_image.cancel_busy"
    CANCEL_REQUEST_KEY_REUSED = "recipe_image.cancel_request_key_reused"
    CANCELLATION_INVALID = "recipe_image.cancellation_invalid"
    CLAIM_LOST = "recipe_image.claim_lost"
    DATABASE_BUSY = "recipe_image.database_busy"
    IDENTITY_CONFLICT = "recipe_image.identity_conflict"
    IDENTITY_INVALID = "recipe_image.identity_invalid"
    INSUFFICIENT_DISK = "recipe_image.insufficient_disk"
    INSUFFICIENT_MEMORY = "recipe_image.insufficient_memory"
    METADATA_REFRESH_FAILED = "recipe_image.metadata_refresh_failed"
    METADATA_REFRESH_UNAVAILABLE = "recipe_image.metadata_refresh_unavailable"
    MODEL_CACHE_FAILED = "recipe_image.model_cache_failed"
    MODEL_CACHE_INVALID = "recipe_image.model_cache_invalid"
    MODEL_CACHE_UNAVAILABLE = "recipe_image.model_cache_unavailable"
    MODEL_CHILD_CANCELLED = "recipe_image.model_child_cancelled"
    MODEL_CHILD_MISSING = "recipe_image.model_child_missing"
    NO_BUILDER = "recipe_image.no_builder"
    NOT_CANCELLABLE = "recipe_image.not_cancellable"
    NOT_RETRYABLE = "recipe_image.not_retryable"
    OPERATION_INVALID = "recipe_image.operation_invalid"
    OPERATION_MISSING = "recipe_image.operation_missing"
    PREPARATION_FAILED = "recipe_image.preparation_failed"
    PREPARATION_EXHAUSTED = "recipe_image.preparation_exhausted"
    PREPARING = "recipe_image.preparing"
    RECIPE_INVALID = "recipe_image.recipe_invalid"
    RECIPE_UNAVAILABLE = "recipe_image.recipe_unavailable"
    REMOVAL_CHOICE_INVALID = "recipe_image.removal_choice_invalid"
    REMOVAL_FAILED = "recipe_image.removal_failed"
    REMOVAL_REFERENCED = "recipe_image.removal_referenced"
    REMOVAL_SCOPE_LIMITED = "recipe_image.removal_scope_limited"
    REQUEST_KEY_REUSED = "recipe_image.request_key_reused"
    RUNTIME_INVALID = "recipe_image.runtime_invalid"
    SELECTOR_AMBIGUOUS = "recipe_image.selector_ambiguous"
    SELECTOR_INVALID = "recipe_image.selector_invalid"
    SELECTOR_MISSING = "recipe_image.selector_missing"
    SOURCE_POLICY_REFUSED = "recipe_image.source_policy_refused"
    SUPERSEDED_BY_NEWER_REVISION = "recipe_image.superseded_by_newer_revision"
    WAITING_FOR_MODEL = "recipe_image.waiting_for_model"
    WAITING_FOR_WORKER = "recipe_image.waiting_for_worker"


class RecipeOperationCode(WireEnum):
    """Recipe operation conflicts."""

    RECIPE_OPERATION_CONFLICT = "recipe.operation_conflict"


class RecipePackageCode(WireEnum):
    """Recipe package download and verification problems."""

    CACHE_UNAVAILABLE = "recipe_package.cache_unavailable"
    DIGEST_MISMATCH = "recipe_package.digest_mismatch"
    DOCUMENT_INCOMPATIBLE = "recipe_package.document_incompatible"
    EXTRACT_INVALID = "recipe_package.extract_invalid"
    NOT_FOUND = "recipe_package.not_found"
    PACKAGE_INVALID = "recipe_package.package_invalid"
    RELEASE_INCOMPLETE = "recipe_package.release_incomplete"
    RELEASE_INVALID = "recipe_package.release_invalid"
    RESPONSE_INVALID = "recipe_package.response_invalid"
    SCHEMA_INCOMPATIBLE = "recipe_package.schema_incompatible"
    SNAPSHOT_CHANGED = "recipe_package.snapshot_changed"
    UNAVAILABLE = "recipe_package.unavailable"
    URI_INVALID = "recipe_package.uri_invalid"
    URL_INSECURE = "recipe_package.url_insecure"
    URL_INVALID = "recipe_package.url_invalid"


class RecipeUpdateCode(WireEnum):
    """Recipe update batch problems."""

    CLAIM_LOST = "recipe_update.claim_lost"
    OBSERVATION_INVALID = "recipe_update.observation_invalid"
    OPERATION_INVALID = "recipe_update.operation_invalid"
    REQUEST_KEY_REUSED = "recipe_update.request_key_reused"
    SCOPE_INVALID = "recipe_update.scope_invalid"
    CANCEL_EFFECT_UNKNOWN = "recipe-update.cancel-effect-unknown"


class ReconcileCode(WireEnum):
    """Why an installation reconcile is blocked."""

    ACTIVE_EFFECT_UNKNOWN = "reconcile.active_effect_unknown"
    AGENT_UNAVAILABLE = "reconcile.agent_unavailable"
    CAPACITY_BUSY = "reconcile.capacity_busy"
    INSTALL_PROVENANCE_MISMATCH = "reconcile.install_provenance_mismatch"
    INSTALL_PROVENANCE_UNAVAILABLE = "reconcile.install_provenance_unavailable"
    INSTALLATION_EFFECT_UNKNOWN = "reconcile.installation_effect_unknown"
    INSTALLATION_IDENTITY_MISMATCH = "reconcile.installation_identity_mismatch"
    INSTALLATION_IDENTITY_UNAVAILABLE = "reconcile.installation_identity_unavailable"
    MEMBERSHIP_CHANGED = "reconcile.membership_changed"
    OPERATION_ACTIVE = "reconcile.operation_active"
    RANK_MEMBERSHIP_CHANGED = "reconcile.rank_membership_changed"
    RECIPE_REVISION_UNAVAILABLE = "reconcile.recipe_revision_unavailable"
    SPEC_IDENTITY_MISMATCH = "reconcile.spec_identity_mismatch"


class ResourcePlanningCode(WireEnum):
    """Resource planning (memory, disk, parallelism) refusals and unknowns."""

    ENVELOPE_EXCEEDS_CAPACITY = "resource.envelope_exceeds_capacity"
    ENVELOPE_UNVERIFIED = "resource.envelope_unverified"
    ESTIMATE_UNCERTAIN = "resource.estimate_uncertain"
    EVIDENCE_INVALID = "resource.evidence_invalid"
    EVIDENCE_UNKNOWN = "resource.evidence_unknown"
    KNOBS_INVALID = "resource.knobs_invalid"
    PARALLELISM_DUPLICATE = "resource.parallelism_duplicate"
    PARALLELISM_INCONSISTENT = "resource.parallelism_inconsistent"
    PARALLELISM_TYPE = "resource.parallelism_type"
    PARALLELISM_UNKNOWN = "resource.parallelism_unknown"
    SETTINGS_KIND_UNKNOWN = "resource.settings_kind_unknown"
    SETTINGS_TYPE = "resource.settings_type"
    SETTINGS_UNKNOWN = "resource.settings_unknown"
    STOP_RELEASE_UNKNOWN = "resource.stop_release_unknown"
    CONTEXT_UNKNOWN = "resource.context_unknown"
    CONTEXT_EVIDENCE_INVALID = "resource.context_evidence_invalid"
    CONTEXT_UNSUPPORTED = "resource.context_unsupported"
    CONTEXT_EVIDENCE_UNKNOWN = "resource.context_evidence_unknown"
    CONCURRENCY_UNKNOWN = "resource.concurrency_unknown"
    CONCURRENCY_EVIDENCE_INVALID = "resource.concurrency_evidence_invalid"
    CONCURRENCY_UNSUPPORTED = "resource.concurrency_unsupported"
    CONCURRENCY_EVIDENCE_UNKNOWN = "resource.concurrency_evidence_unknown"
    BATCH_UNKNOWN = "resource.batch_unknown"
    BATCH_EVIDENCE_INVALID = "resource.batch_evidence_invalid"
    BATCH_UNSUPPORTED = "resource.batch_unsupported"
    BATCH_EVIDENCE_UNKNOWN = "resource.batch_evidence_unknown"


class ResourceTerm(WireEnum):
    """The effective settings whose capacity cost the resource planner derives."""

    CONTEXT = "context"
    CONCURRENCY = "concurrency"
    BATCH = "batch"


class ResourceTermProblem(WireEnum):
    """What is wrong with the evidence for one resource term."""

    UNKNOWN = "unknown"
    EVIDENCE_INVALID = "evidence_invalid"
    UNSUPPORTED = "unsupported"
    EVIDENCE_UNKNOWN = "evidence_unknown"


class RunDegradedReason(WireEnum):
    """Why a run is shown degraded in the fleet projection."""

    EXTERNAL_MEMBER = "external-member"
    MAPPING_INCOMPLETE = "mapping-incomplete"
    MISSING_RANKS = "missing-ranks"
    UNEXPECTED_RANKS = "unexpected-ranks"
    RANK_MEMBERSHIP_MISMATCH = "rank-membership-mismatch"
    RUN_NOT_RUNNING = "run-not-running"
    RANK_NOT_RUNNING = "rank-not-running"
    RANK_STALE = "rank-stale"
    ROUTE_NOT_PUBLISHED = "route-not-published"


class RunSwitchCode(WireEnum):
    """Run/Switch phase blockers, waits and failure codes."""

    ACTIVE_RUN_CONFLICT = "run-switch.active-run-conflict"
    ADVANCE_FAILED = "run-switch.advance-failed"
    AGENT_UPGRADE_REQUIRED = "run-switch.agent-upgrade-required"
    ARTIFACT_IDENTITY_UNKNOWN = "run-switch.artifact-identity-unknown"
    ARTIFACT_INSPECTION_UNAVAILABLE = "run-switch.artifact-inspection-unavailable"
    ARTIFACT_MANIFEST_UNKNOWN = "run-switch.artifact-manifest-unknown"
    ARTIFACT_PHASE_EXECUTOR_UNAVAILABLE = (
        "run-switch.artifact-phase-executor-unavailable"
    )
    ARTIFACT_VERIFICATION_RESULT_INVALID = (
        "run-switch.artifact-verification-result-invalid"
    )
    CLEANUP_RECLAIM_EVIDENCE_INVALID = "run-switch.cleanup-reclaim-evidence-invalid"
    CLEANUP_RECLAIMED_BYTES_EXCEED_PLAN = (
        "run-switch.cleanup-reclaimed-bytes-exceed-plan"
    )
    CLEANUP_REFERENCE_PROTECTION_EVIDENCE_INVALID = (
        "run-switch.cleanup-reference-protection-evidence-invalid"
    )
    CLEANUP_REFERENCE_PROTECTION_OVERLAP = (
        "run-switch.cleanup-reference-protection-overlap"
    )
    CLEANUP_SCOPE_INVALID = "run-switch.cleanup-scope-invalid"
    CONTAINER_BUILD_EVIDENCE_INVALID = "run-switch.container-build-evidence-invalid"
    CONTAINER_BUILD_EXECUTOR_UNAVAILABLE = (
        "run-switch.container-build-executor-unavailable"
    )
    CONTAINER_BUILD_IDENTITY_UNAVAILABLE = (
        "run-switch.container-build-identity-unavailable"
    )
    CONTAINER_BUILD_PARENT_CHANGED = "run-switch.container-build-parent-changed"
    CONTAINER_BUILD_PARENT_INVALID = "run-switch.container-build-parent-invalid"
    CONTAINER_BUILD_PLAN_INVALID = "run-switch.container-build-plan-invalid"
    CONTAINER_BUILD_RECEIPT_UNAVAILABLE = (
        "run-switch.container-build-receipt-unavailable"
    )
    CONTAINER_BUILD_REQUIRED = "run-switch.container-build-required"
    CONTAINER_BUILD_STATE_INVALID = "run-switch.container-build-state-invalid"
    CONTAINER_BUILD_UNAVAILABLE = "run-switch.container-build-unavailable"
    CROSS_GROUP_CONFLICT = "run-switch.cross-group_conflict"
    DISK_ENVELOPE_INVALID = "run-switch.disk-envelope-invalid"
    DISK_EVICTION_PLANNED = "run-switch.disk-eviction-planned"
    EFFECT_UNCERTAIN = "run-switch.effect-uncertain"
    FINAL_VERIFICATION = "run-switch.final-verification"
    FINAL_VERIFICATION_CLOCK_INVALID = "run-switch.final-verification-clock-invalid"
    FINAL_VERIFICATION_FAILED = "run-switch.final-verification-failed"
    FINAL_VERIFICATION_TIMEOUT = "run-switch.final-verification-timeout"
    FINAL_VERIFICATION_UNAVAILABLE = "run-switch.final-verification-unavailable"
    INSTALL_EXECUTOR_UNAVAILABLE = "run-switch.install-executor-unavailable"
    INSTALL_PREPARATION_UNAVAILABLE = "run-switch.install-preparation-unavailable"
    INSTALLATION_HANDOFF_UNAVAILABLE = "run-switch.installation-handoff-unavailable"
    INSTALLATION_IDENTITY_CHANGED = "run-switch.installation-identity-changed"
    INSTALLATION_IDENTITY_UNAVAILABLE = "run-switch.installation-identity-unavailable"
    INSTALLATION_MEMBERSHIP_CHANGED = "run-switch.installation-membership-changed"
    INSTALLATION_PREPARATION_UNAVAILABLE = (
        "run-switch.installation-preparation-unavailable"
    )
    INSTALLATION_VERIFICATION_FAILED = "run-switch.installation-verification-failed"
    INSTALLATION_VERIFICATION_UNAVAILABLE = (
        "run-switch.installation-verification-unavailable"
    )
    INSUFFICIENT_DISK = "run-switch.insufficient-disk"
    INSUFFICIENT_MEMORY = "run-switch.insufficient-memory"
    INTERFACE_INVALID = "run-switch.interface-invalid"
    INVENTORY_STALE = "run-switch.inventory-stale"
    INVENTORY_UNKNOWN = "run-switch.inventory-unknown"
    MAPPING_GROUP_MISMATCH = "run-switch.mapping_group_mismatch"
    MAPPING_INVALID = "run-switch.mapping_invalid"
    MAPPING_MATERIALIZATION_UNAVAILABLE = (
        "run-switch.mapping_materialization_unavailable"
    )
    MEMORY_ENVELOPE_INVALID = "run-switch.memory-envelope-invalid"
    MODEL_DOWNLOAD_ARTIFACT_SET_MISMATCH = (
        "run-switch.model-download-artifact-set-mismatch"
    )
    MODEL_DOWNLOAD_BYTE_EVIDENCE_MISMATCH = (
        "run-switch.model-download-byte-evidence-mismatch"
    )
    MODEL_DOWNLOAD_COVERAGE_INCOMPLETE = "run-switch.model-download-coverage-incomplete"
    MODEL_RECIPE_MISMATCH = "run-switch.model_recipe_mismatch"
    MODEL_REVISION_UNAVAILABLE = "run-switch.model_revision_unavailable"
    NAS_COVERAGE_UNKNOWN = "run-switch.nas-coverage-unknown"
    NAS_DOWNLOAD_BLOCKED = "run-switch.nas-download-blocked"
    NAS_DOWNLOAD_REQUIRED = "run-switch.nas-download-required"
    OPTION_INVALID = "run-switch.option_invalid"
    PHASE_RETRY = "run-switch.phase-retry"
    PLAN_REFRESH_UNAVAILABLE = "run-switch.plan-refresh-unavailable"
    PLAN_TARGETS_CHANGED = "run-switch.plan-targets-changed"
    POST_STOP_INVENTORY_PENDING = "run-switch.post-stop-inventory-pending"
    POST_STOP_MEMORY_POOL_CHANGED = "run-switch.post-stop-memory-pool-changed"
    PREFLIGHT_RECIPE_CHANGED = "run-switch.preflight-recipe-changed"
    PREPARE_SUBPHASE_UNSUPPORTED = "run-switch.prepare-subphase-unsupported"
    PROFILE_INCOMPLETE_MULTI_SPARK_MODEL = (
        "run-switch.profile.incomplete_multi_spark_model"
    )
    PROFILE_STOP_SCOPE_CHANGED = "run-switch.profile_stop_scope_changed"
    RECEIPT_INVALID = "run-switch.receipt_invalid"
    RECIPE_BUILD_COMPATIBILITY_UNKNOWN = "run-switch.recipe-build-compatibility-unknown"
    RECIPE_BUILD_INCOMPATIBLE = "run-switch.recipe-build-incompatible"
    RECIPE_BUILD_UNAVAILABLE = "run-switch.recipe-build-unavailable"
    RECIPE_DEPENDENCIES_UNAVAILABLE = "run-switch.recipe_dependencies_unavailable"
    RECIPE_DIGEST_CHANGED = "run-switch.recipe_digest_changed"
    RECIPE_UNRESOLVED = "run-switch.recipe_unresolved"
    RECONCILIATION_ASSESSMENT_UNAVAILABLE = (
        "run-switch.reconciliation-assessment-unavailable"
    )
    RECONCILIATION_AUTHORITY_UNAVAILABLE = (
        "run-switch.reconciliation-authority-unavailable"
    )
    RECONCILIATION_PREREQUISITE = "run-switch.reconciliation-prerequisite"
    RECONCILIATION_RECEIPTS_RETAINED = "run-switch.reconciliation-receipts-retained"
    RECONCILIATION_STATE_VERIFICATION_FAILED = (
        "run-switch.reconciliation-state-verification-failed"
    )
    RECONCILIATION_VERIFICATION_FAILED = "run-switch.reconciliation-verification-failed"
    REQUEST_KEY_REUSED_DIFFERENTLY = "run-switch.request_key_reused_differently"
    RESOURCE_CONTRACT_INVALID = "run-switch.resource-contract-invalid"
    RESOURCE_INSUFFICIENT = "run-switch.resource.insufficient"
    RESOURCE_INSUFFICIENT_CAPACITY = "run-switch.resource.insufficient_capacity"
    RESOURCE_INSUFFICIENT_CAPACITY_AFTER_STOP = (
        "run-switch.resource.insufficient_capacity_after_stop"
    )
    RESOURCE_INSUFFICIENT_RESERVATION_BUDGET = (
        "run-switch.resource.insufficient_reservation_budget"
    )
    RESOURCE_RESIDENT_USAGE_UNKNOWN = "run-switch.resource.resident_usage_unknown"
    RUN_NOT_ACTIVE = "run-switch.run-not-active"
    RUN_ADMISSION_BLOCKED = "run-switch.run_admission_blocked"
    RUN_ADMISSION_UNAVAILABLE = "run-switch.run_admission_unavailable"
    RUNTIME_BUILD_VERIFICATION_MISMATCH = (
        "run-switch.runtime-build-verification-mismatch"
    )
    RUNTIME_IMAGE_AUTHORIZATION_MISMATCH = (
        "run-switch.runtime-image-authorization-mismatch"
    )
    RUNTIME_IMAGE_EXECUTOR_UNAVAILABLE = "run-switch.runtime-image-executor-unavailable"
    RUNTIME_IMAGE_OWNER_CHANGED = "run-switch.runtime-image-owner-changed"
    RUNTIME_IMAGE_PREPARATION_LAYOUT_MISMATCH = (
        "run-switch.runtime-image-preparation-layout-mismatch"
    )
    RUNTIME_IMAGE_PREPARATION_RECEIPT_INVALID = (
        "run-switch.runtime-image-preparation-receipt-invalid"
    )
    RUNTIME_IMAGE_PREPARING = "run-switch.runtime-image-preparing"
    RUNTIME_IMAGE_REFERENCE_IDENTITY_MISMATCH = (
        "run-switch.runtime-image-reference-identity-mismatch"
    )
    RUNTIME_IMAGE_WAITING_WITHOUT_CHILD = (
        "run-switch.runtime-image-waiting-without-child"
    )
    SPARK_UNAVAILABLE = "run-switch.spark-unavailable"
    START_OBSERVATION = "run-switch.start-observation"
    START_OBSERVATION_EXPIRED = "run-switch.start-observation-expired"
    START_INSTALLATION_UNAVAILABLE = "run-switch.start_installation_unavailable"
    STOP_PLAN_UNAVAILABLE = "run-switch.stop-plan-unavailable"
    STOP_STILL_UNRESOLVED_AFTER_CANCELLATION = (
        "run-switch.stop-still-unresolved-after-cancellation"
    )
    STOP_TARGET_DISAPPEARED = "run-switch.stop-target-disappeared"
    STOPPED_RUN_IDENTITY_CHANGED = "run-switch.stopped-run-identity-changed"
    STOPPED_RUN_MEMBERSHIP_CHANGED = "run-switch.stopped-run-membership-changed"
    TARGET_NOT_ACTIVE = "run-switch.target-not-active"
    TRANSFER_BYTE_EVIDENCE_INVALID = "run-switch.transfer-byte-evidence-invalid"
    UNINSTALL_ASSESSMENT_UNAVAILABLE = "run-switch.uninstall-assessment-unavailable"
    UNINSTALL_BLOCKED = "run-switch.uninstall-blocked"
    UNINSTALL_ISSUED_PREREQUISITE = "run-switch.uninstall-issued-prerequisite"
    UNINSTALL_TARGET_UNAVAILABLE = "run-switch.uninstall_target_unavailable"
    WAITING = "run-switch.waiting"
    REASON_UNCLASSIFIED = "run-switch.reason-unclassified"
    CANCEL_EFFECT_UNKNOWN = "run-switch.cancel-effect-unknown"
    CONTAINER_BUILD_START_UNAVAILABLE = "run-switch.container-build-start-unavailable"
    DISTRIBUTED_RECOVERY_ACTIVE = "run-switch.distributed-recovery-active"
    FINAL_OWNER_STATE_UNKNOWN = "run-switch.final-owner-state-unknown"
    FINAL_VERIFICATION_EXPIRED = "run-switch.final-verification-expired"
    INSTALL_PLAN_UNAVAILABLE = "run-switch.install-plan-unavailable"
    INSTALL_PREFLIGHT_EXPIRED = "run-switch.install-preflight-expired"
    INSTALL_PREPARATION_FAILED = "run-switch.install-preparation-failed"
    INSTALL_START_FAILED = "run-switch.install-start-failed"
    INSTALLATION_HANDOFF_INCONSISTENT = "run-switch.installation-handoff-inconsistent"
    PLAN_BLOCKED = "run-switch.plan_blocked"
    RECONCILIATION_START_FAILED = "run-switch.reconciliation-start-failed"
    ROUTE_HEALTH_RECOVERY_ACTIVE = "run-switch.route-health-recovery-active"
    ROUTE_OWNER_FAILED = "run-switch.route-owner-failed"
    ROUTE_PUBLICATION_PENDING = "run-switch.route-publication-pending"
    ROUTE_WITHDRAWN_OWNER_UNKNOWN = "run-switch.route-withdrawn-owner-unknown"
    RUN_OWNER_ACTIVE = "run-switch.run-owner-active"
    RUN_OWNER_TERMINAL = "run-switch.run-owner-terminal"
    STALE_PLAN = "run-switch.stale_plan"
    STOP_VERIFICATION_PENDING = "run-switch.stop-verification-pending"
    SUPERSEDED = "run-switch.superseded"
    UNINSTALL_ABANDON_FAILED = "run-switch.uninstall-abandon-failed"
    UNINSTALL_START_FAILED = "run-switch.uninstall-start-failed"
    RECIPE_STOP_ISSUED_PENDING = "run-switch.recipe.stop-issued-pending"
    RECIPE_INSTALL_ISSUED_PENDING = "run-switch.recipe.install-issued-pending"
    RECIPE_UNINSTALL_ISSUED_PENDING = "run-switch.recipe.uninstall-issued-pending"
    RECIPE_RECONCILE_ISSUED_PENDING = "run-switch.recipe.reconcile-issued-pending"
    ARTIFACT_JOB_CANCELLATION_ISSUED_PENDING = (
        "run-switch.artifact-job-cancellation-issued-pending"
    )
    TRANSFER_EXECUTOR_UNAVAILABLE = "run-switch.transfer-executor-unavailable"
    TRANSFER_WAITING_WITHOUT_CHILD = "run-switch.transfer-waiting-without-child"
    TRANSFER_RETURNED_NO_EVIDENCE = "run-switch.transfer-returned-no-evidence"
    VERIFY_EXECUTOR_UNAVAILABLE = "run-switch.verify-executor-unavailable"
    VERIFY_WAITING_WITHOUT_CHILD = "run-switch.verify-waiting-without-child"
    VERIFY_RETURNED_NO_EVIDENCE = "run-switch.verify-returned-no-evidence"
    CLEANUP_EXECUTOR_UNAVAILABLE = "run-switch.cleanup-executor-unavailable"
    CLEANUP_WAITING_WITHOUT_CHILD = "run-switch.cleanup-waiting-without-child"
    CLEANUP_RETURNED_NO_EVIDENCE = "run-switch.cleanup-returned-no-evidence"
    RESOURCE_CAPACITY_UNKNOWN = "run-switch.resource.capacity_unknown"
    RESOURCE_ENVELOPE_EXCEEDS_CAPACITY = "run-switch.resource.envelope_exceeds_capacity"
    RESOURCE_ENVELOPE_UNVERIFIED = "run-switch.resource.envelope_unverified"
    RESOURCE_ESTIMATE_UNCERTAIN = "run-switch.resource.estimate_uncertain"
    RESOURCE_EVIDENCE_INVALID = "run-switch.resource.evidence_invalid"
    RESOURCE_EVIDENCE_UNKNOWN = "run-switch.resource.evidence_unknown"
    RESOURCE_KNOBS_INVALID = "run-switch.resource.knobs_invalid"
    RESOURCE_PARALLELISM_DUPLICATE = "run-switch.resource.parallelism_duplicate"
    RESOURCE_PARALLELISM_INCONSISTENT = "run-switch.resource.parallelism_inconsistent"
    RESOURCE_PARALLELISM_TYPE = "run-switch.resource.parallelism_type"
    RESOURCE_PARALLELISM_UNKNOWN = "run-switch.resource.parallelism_unknown"
    RESOURCE_SETTINGS_KIND_UNKNOWN = "run-switch.resource.settings_kind_unknown"
    RESOURCE_SETTINGS_TYPE = "run-switch.resource.settings_type"
    RESOURCE_SETTINGS_UNKNOWN = "run-switch.resource.settings_unknown"
    RESOURCE_STOP_RELEASE_UNKNOWN = "run-switch.resource.stop_release_unknown"
    RECONCILE_ACTIVE_EFFECT_UNKNOWN = "run-switch.reconcile.active_effect_unknown"
    RECONCILE_AGENT_UNAVAILABLE = "run-switch.reconcile.agent_unavailable"
    RECONCILE_CAPACITY_BUSY = "run-switch.reconcile.capacity_busy"
    RECONCILE_INSTALL_PROVENANCE_MISMATCH = (
        "run-switch.reconcile.install_provenance_mismatch"
    )
    RECONCILE_INSTALL_PROVENANCE_UNAVAILABLE = (
        "run-switch.reconcile.install_provenance_unavailable"
    )
    RECONCILE_INSTALLATION_EFFECT_UNKNOWN = (
        "run-switch.reconcile.installation_effect_unknown"
    )
    RECONCILE_INSTALLATION_IDENTITY_MISMATCH = (
        "run-switch.reconcile.installation_identity_mismatch"
    )
    RECONCILE_INSTALLATION_IDENTITY_UNAVAILABLE = (
        "run-switch.reconcile.installation_identity_unavailable"
    )
    RECONCILE_MEMBERSHIP_CHANGED = "run-switch.reconcile.membership_changed"
    RECONCILE_OPERATION_ACTIVE = "run-switch.reconcile.operation_active"
    RECONCILE_RANK_MEMBERSHIP_CHANGED = "run-switch.reconcile.rank_membership_changed"
    RECONCILE_RECIPE_REVISION_UNAVAILABLE = (
        "run-switch.reconcile.recipe_revision_unavailable"
    )
    RECONCILE_SPEC_IDENTITY_MISMATCH = "run-switch.reconcile.spec_identity_mismatch"
    STOP_CAPACITY_RELEASE_DEFERRED = "run-switch.stop.capacity_release_deferred"
    STOP_RANK_MEMBERSHIP_CHANGED = "run-switch.stop.rank_membership_changed"
    STOP_RESERVATION_MEMBERSHIP_CHANGED = (
        "run-switch.stop.reservation_membership_changed"
    )
    STOP_RUN_NOT_STOPPABLE = "run-switch.stop.run_not_stoppable"
    STOP_TARGET_SCOPE_CHANGED = "run-switch.stop.target_scope_changed"
    UNINSTALL_ABANDON_NEVER_INSTALLED = "run-switch.uninstall.abandon-never-installed"
    UNINSTALL_ACTIVE_RUN = "run-switch.uninstall.active_run"
    UNINSTALL_ACTIVE_RUNS_TRUNCATED = "run-switch.uninstall.active_runs_truncated"
    UNINSTALL_BYTES_UNKNOWN = "run-switch.uninstall.bytes_unknown"
    UNINSTALL_INSTALLATION_NOT_UNINSTALLABLE = (
        "run-switch.uninstall.installation_not_uninstallable"
    )
    UNINSTALL_OPERATION_ACTIVE = "run-switch.uninstall.operation_active"
    UNINSTALL_RANK_MEMBERSHIP_CHANGED = "run-switch.uninstall.rank_membership_changed"


class RuntimeImageCode(WireEnum):
    """Runtime image preparation, receipt and registry problems."""

    DESTINATION_FORBIDDEN = "registry.destination_forbidden"
    DIGEST_MISMATCH = "registry.digest_mismatch"
    REDIRECT_FORBIDDEN = "registry.redirect_forbidden"
    ARCHITECTURE_MISMATCH = "runtime_image.architecture_mismatch"
    ARCHITECTURE_MISSING = "runtime_image.architecture_missing"
    ARCHIVE_CONFLICT = "runtime_image.archive_conflict"
    ARCHIVE_INVALID = "runtime_image.archive_invalid"
    ARCHIVE_MISMATCH = "runtime_image.archive_mismatch"
    ARCHIVE_SIZE_MISMATCH = "runtime_image.archive_size_mismatch"
    ARCHIVE_UNAVAILABLE = "runtime_image.archive_unavailable"
    BUILD_ARCHIVE_DIGEST = "runtime_image.build_archive_digest"
    BUILD_DIGEST = "runtime_image.build_digest"
    BUILD_ID = "runtime_image.build_id"
    BUILD_INCOMPLETE = "runtime_image.build_incomplete"
    CACHE_MISSING = "runtime_image.cache_missing"
    CONFIG_MISSING = "runtime_image.config_missing"
    DIGEST_INVALID = "runtime_image.digest_invalid"
    DIGEST_MISMATCH_ = "runtime_image.digest_mismatch"
    EVIDENCE_INVALID = "runtime_image.evidence_invalid"
    IDENTITY_INVALID = "runtime_image.identity_invalid"
    IMAGE_UNPINNED = "runtime_image.image_unpinned"
    INSPECT_INVALID = "runtime_image.inspect_invalid"
    INSUFFICIENT_DISK = "runtime_image.insufficient_disk"
    INTERFACE_MISMATCH = "runtime_image.interface_mismatch"
    INTERFACE_MISSING = "runtime_image.interface_missing"
    LOCK_UNAVAILABLE = "runtime_image.lock_unavailable"
    PUBLICATION_CONTENDED = "runtime_image.publication_contended"
    RECEIPT_CONTRACT_NEWER = "runtime_image.receipt_contract_newer"
    RECEIPT_IDENTITY_CONFLICT = "runtime_image.receipt_identity_conflict"
    RECEIPT_IDENTITY_INVALID = "runtime_image.receipt_identity_invalid"
    RECEIPT_INVALID = "runtime_image.receipt_invalid"
    RECEIPT_PERSISTENCE_FAILED = "runtime_image.receipt_persistence_failed"
    RECEIPT_UNAVAILABLE = "runtime_image.receipt_unavailable"
    RECEIPT_WRITE_FAILED = "runtime_image.receipt_write_failed"
    RECIPE_INVALID = "runtime_image.recipe_invalid"
    REFERENCE_INTENT_INVALID = "runtime_image.reference_intent_invalid"
    REMOVAL_STORAGE_FAILED = "runtime_image.removal_storage_failed"
    RUNTIME_INVALID = "runtime_image.runtime_invalid"
    SOURCE_MISMATCH = "runtime_image.source_mismatch"
    TRANSFER_CONTENDED = "runtime_image.transfer_contended"


class RuntimePreflightCode(WireEnum):
    """Runtime preflight blockers."""

    CHILD_MISSING = "runtime_preflight.child_missing"
    EXECUTION_FAILED = "runtime_preflight.execution_failed"
    HOST_CHANGED = "runtime_preflight.host_changed"
    NODE_REVOKED = "runtime_preflight.node_revoked"
    OPERATION_MISSING = "runtime_preflight.operation_missing"
    RECEIPT_INVALID = "runtime_preflight.receipt_invalid"
    RECEIPT_MISSING = "runtime_preflight.receipt_missing"
    REQUIRED = "runtime_preflight.required"
    REQUIREMENT_UNKNOWN = "runtime_preflight.requirement_unknown"
    REQUIREMENTS_CHANGED = "runtime_preflight.requirements_changed"
    STALE = "runtime_preflight.stale"
    NODE_MISSING = "runtime_preflight.node_missing"
    CAPABILITY_FAILED = "runtime_preflight.capability_failed"


class RuntimePreflightFindingCode(WireEnum):
    """Why one runtime preflight capability passed, failed or stayed unknown, as the agent reports it.

    The agent builds every finding from a member; the Controller reads a code an
    older agent sent as free text through :func:`adopt_preflight_finding_code`.
    """

    ARCHITECTURE_MISMATCH = "preflight_finding.architecture_mismatch"
    AVAILABLE = "preflight_finding.available"
    BUILD_STEP_FAILED = "preflight_finding.build_step_failed"
    CAPABILITIES_NOT_ZERO = "preflight_finding.capabilities_not_zero"
    CONTROLLER_UNREACHABLE = "preflight_finding.controller_unreachable"
    DEADLINE_EXCEEDED = "preflight_finding.deadline_exceeded"
    DIAGNOSTIC_LIMIT_EXCEEDED = "preflight_finding.diagnostic_limit_exceeded"
    DIRECTORY_NOT_WRITABLE = "preflight_finding.directory_not_writable"
    DISK_RESERVE_INSUFFICIENT = "preflight_finding.disk_reserve_insufficient"
    FABRIC_REQUIREMENT_UNVERIFIED = "preflight_finding.fabric_requirement_unverified"
    HELPER_CALL_JOIN_FAILED = "preflight_finding.helper_call_join_failed"
    HELPER_CAPABILITIES_NOT_ZERO = "preflight_finding.helper_capabilities_not_zero"
    HELPER_GRANT_INVALID = "preflight_finding.helper_grant_invalid"
    HELPER_GRANT_NODE_MISMATCH = "preflight_finding.helper_grant_node_mismatch"
    HELPER_GRANT_UNAUTHORIZED = "preflight_finding.helper_grant_unauthorized"
    HELPER_GRANT_UNAVAILABLE = "preflight_finding.helper_grant_unavailable"
    HELPER_IMAGE_IMPORT_FAILED = "preflight_finding.helper_image_import_failed"
    HELPER_INSPECTION_OUTCOME_INVALID = (
        "preflight_finding.helper_inspection_outcome_invalid"
    )
    HELPER_INSTALLATION_RECONCILIATION_BUSY = (
        "preflight_finding.helper_installation_reconciliation_busy"
    )
    HELPER_INSTALLATION_RECONCILIATION_STORAGE_UNAVAILABLE = (
        "preflight_finding.helper_installation_reconciliation_storage_unavailable"
    )
    HELPER_IO_FAILED = "preflight_finding.helper_io_failed"
    HELPER_MESSAGE_FRAMING_INVALID = "preflight_finding.helper_message_framing_invalid"
    HELPER_MOUNT_NAMESPACE_UNAVAILABLE = (
        "preflight_finding.helper_mount_namespace_unavailable"
    )
    HELPER_NO_NEW_PRIVILEGES_UNAVAILABLE = (
        "preflight_finding.helper_no_new_privileges_unavailable"
    )
    HELPER_OPERATION_COMMAND_FAILED = (
        "preflight_finding.helper_operation_command_failed"
    )
    HELPER_OPERATION_FAILED = "preflight_finding.helper_operation_failed"
    HELPER_OPERATION_INVALID = "preflight_finding.helper_operation_invalid"
    HELPER_OPERATION_INVALID_ARTIFACT = (
        "preflight_finding.helper_operation_invalid_artifact"
    )
    HELPER_OPERATION_IO = "preflight_finding.helper_operation_io"
    HELPER_OPERATION_STOP_UNCERTAIN = (
        "preflight_finding.helper_operation_stop_uncertain"
    )
    HELPER_OPERATION_UNSAFE_PATH = "preflight_finding.helper_operation_unsafe_path"
    HELPER_OUTCOME_MALFORMED = "preflight_finding.helper_outcome_malformed"
    HELPER_PEER_IDENTITY_INVALID = "preflight_finding.helper_peer_identity_invalid"
    HELPER_PROBE_CLEANUP_FAILED = "preflight_finding.helper_probe_cleanup_failed"
    HELPER_PROBE_INVALID_RESULT = "preflight_finding.helper_probe_invalid_result"
    HELPER_PROC_UNAVAILABLE = "preflight_finding.helper_proc_unavailable"
    HELPER_PROTOCOL_INVALID = "preflight_finding.helper_protocol_invalid"
    HELPER_REJECTION_MALFORMED = "preflight_finding.helper_rejection_malformed"
    HELPER_REQUEST_ARGUMENTS_PRESENCE_INVALID = (
        "preflight_finding.helper_request_arguments_presence_invalid"
    )
    HELPER_REQUEST_ARGUMENT_NUL_BYTE = (
        "preflight_finding.helper_request_argument_nul_byte"
    )
    HELPER_REQUEST_ATTEMPT_INVALID = "preflight_finding.helper_request_attempt_invalid"
    HELPER_REQUEST_BYTES_INVALID = "preflight_finding.helper_request_bytes_invalid"
    HELPER_REQUEST_DOCUMENT_INVALID = (
        "preflight_finding.helper_request_document_invalid"
    )
    HELPER_REQUEST_ENCODING_INVALID = (
        "preflight_finding.helper_request_encoding_invalid"
    )
    HELPER_REQUEST_INSTALLATION_IDENTITY_INVALID = (
        "preflight_finding.helper_request_installation_identity_invalid"
    )
    HELPER_REQUEST_INVALID = "preflight_finding.helper_request_invalid"
    HELPER_REQUEST_LEDGER_FAILED = "preflight_finding.helper_request_ledger_failed"
    HELPER_REQUEST_PLAN_BINDING_INVALID = (
        "preflight_finding.helper_request_plan_binding_invalid"
    )
    HELPER_REQUEST_PLAN_BYTES_INVALID = (
        "preflight_finding.helper_request_plan_bytes_invalid"
    )
    HELPER_REQUEST_REPLAYED = "preflight_finding.helper_request_replayed"
    HELPER_REQUEST_SCHEMA_VERSION_INVALID = (
        "preflight_finding.helper_request_schema_version_invalid"
    )
    HELPER_REQUEST_STORAGE_INVALID = "preflight_finding.helper_request_storage_invalid"
    HELPER_RESPONSE_UNBOUND = "preflight_finding.helper_response_unbound"
    HELPER_RUNTIME_ENDPOINT_FIREWALL_REJECTED = (
        "preflight_finding.helper_runtime_endpoint_firewall_rejected"
    )
    HELPER_RUNTIME_FABRIC_FIREWALL_REJECTED = (
        "preflight_finding.helper_runtime_fabric_firewall_rejected"
    )
    HELPER_RUNTIME_FABRIC_UNAVAILABLE = (
        "preflight_finding.helper_runtime_fabric_unavailable"
    )
    HELPER_RUNTIME_IMAGE_IDENTITY_INVALID = (
        "preflight_finding.helper_runtime_image_identity_invalid"
    )
    HELPER_RUNTIME_IMAGE_INSPECT_FAILED = (
        "preflight_finding.helper_runtime_image_inspect_failed"
    )
    HELPER_RUNTIME_IMAGE_LOAD_FAILED = (
        "preflight_finding.helper_runtime_image_load_failed"
    )
    HELPER_RUNTIME_IMAGE_RECEIPT_FAILED = (
        "preflight_finding.helper_runtime_image_receipt_failed"
    )
    HELPER_RUNTIME_PROCESS_EXITED = "preflight_finding.helper_runtime_process_exited"
    HELPER_RUNTIME_RUN_MISSING = "preflight_finding.helper_runtime_run_missing"
    HELPER_SANDBOX_RUN_FAILED = "preflight_finding.helper_sandbox_run_failed"
    HELPER_STOP_UNCERTAIN = "preflight_finding.helper_stop_uncertain"
    HELPER_SYSTEM_CLOCK_INVALID = "preflight_finding.helper_system_clock_invalid"
    HELPER_TEMPORARY_DIRECTORY_UNAVAILABLE = (
        "preflight_finding.helper_temporary_directory_unavailable"
    )
    MEMORY_LIMIT_EXCEEDED = "preflight_finding.memory_limit_exceeded"
    MOUNT_NAMESPACE_UNAVAILABLE = "preflight_finding.mount_namespace_unavailable"
    NONZERO_WITHOUT_OUTPUT = "preflight_finding.nonzero_without_output"
    NO_NEW_PRIVILEGES_UNAVAILABLE = "preflight_finding.no_new_privileges_unavailable"
    OCI_RUNTIME_UNAVAILABLE = "preflight_finding.oci_runtime_unavailable"
    PATCH_REJECTED = "preflight_finding.patch_rejected"
    PERMISSION_DENIED = "preflight_finding.permission_denied"
    PROC_MOUNT_DENIED = "preflight_finding.proc_mount_denied"
    PROC_UNAVAILABLE = "preflight_finding.proc_unavailable"
    RUNROOT_EXCEEDS_50_BYTES = "preflight_finding.runroot_exceeds_50_bytes"
    SIGNED_HELPER_PROBE_REQUIRED = "preflight_finding.signed_helper_probe_required"
    STORAGE_DRIVER_FAILURE = "preflight_finding.storage_driver_failure"
    SUBORDINATE_ID_MAPPING_UNAVAILABLE = (
        "preflight_finding.subordinate_id_mapping_unavailable"
    )
    SUBPROCESS_UNAVAILABLE = "preflight_finding.subprocess_unavailable"
    SYSTEMD_SCOPE_FAILURE = "preflight_finding.systemd_scope_failure"
    TEMPORARY_DIRECTORY_UNAVAILABLE = (
        "preflight_finding.temporary_directory_unavailable"
    )
    TEMPORARY_STORAGE_EXHAUSTED = "preflight_finding.temporary_storage_exhausted"
    UNCLASSIFIED = "preflight_finding.unclassified"
    UNCLASSIFIED_PODMAN_BUILD_FAILURE = (
        "preflight_finding.unclassified_podman_build_failure"
    )
    USER_NAMESPACE_DENIED = "preflight_finding.user_namespace_denied"
    USER_SERVICE_MANAGER_UNAVAILABLE = (
        "preflight_finding.user_service_manager_unavailable"
    )


class SourceBundleCode(WireEnum):
    """Recipe source bundle validation problems."""

    ARCHIVE_TOO_LARGE = "bundle.archive_too_large"
    DIGEST_INVALID = "bundle.digest_invalid"
    DIGEST_MISMATCH = "bundle.digest_mismatch"
    DUPLICATE_PATH = "bundle.duplicate_path"
    EMPTY = "bundle.empty"
    ENTRY_FORBIDDEN = "bundle.entry_forbidden"
    EXPANDED_TOO_LARGE = "bundle.expanded_too_large"
    FILE_INVALID = "bundle.file_invalid"
    FILE_TOO_LARGE = "bundle.file_too_large"
    INVALID_ARCHIVE = "bundle.invalid_archive"
    MANIFEST_INVALID = "bundle.manifest_invalid"
    METADATA_MISMATCH = "bundle.metadata_mismatch"
    NOT_FOUND = "bundle.not_found"
    PATH_FORBIDDEN = "bundle.path_forbidden"
    PATH_TOO_LONG = "bundle.path_too_long"
    READ_FAILED = "bundle.read_failed"
    SIZE_MISMATCH = "bundle.size_mismatch"
    STORAGE_COLLISION = "bundle.storage_collision"
    STORAGE_CONFLICT = "bundle.storage_conflict"
    STORAGE_UNAVAILABLE = "bundle.storage_unavailable"
    TOO_MANY_FILES = "bundle.too_many_files"
    DIGEST_MISMATCH_ = "source.digest_mismatch"


class SourcePolicyCode(WireEnum):
    """Findings of the build source policy (Dockerfile and Compose rules)."""

    CAPABILITIES = "compose.capabilities"
    DEVICES = "compose.devices"
    HOST_BIND = "compose.host_bind"
    HOST_NAMESPACE = "compose.host_namespace"
    INVALID = "compose.invalid"
    PRIVILEGED = "compose.privileged"
    SERVICE_INVALID = "compose.service_invalid"
    TOO_LARGE = "compose.too_large"
    UNCONFINED = "compose.unconfined"
    VOLUMES_INVALID = "compose.volumes_invalid"
    ADD_FORBIDDEN = "dockerfile.add_forbidden"
    BASE_PLACEHOLDER = "dockerfile.base_placeholder"
    BASE_UNPINNED = "dockerfile.base_unpinned"
    BUILD_PRIVILEGE = "dockerfile.build_privilege"
    COPY_BASE_PLACEHOLDER = "dockerfile.copy_base_placeholder"
    COPY_BASE_UNPINNED = "dockerfile.copy_base_unpinned"
    COPY_INVALID = "dockerfile.copy_invalid"
    COPY_PATH = "dockerfile.copy_path"
    FROM_MISSING = "dockerfile.from_missing"
    HEREDOC_FORBIDDEN = "dockerfile.heredoc_forbidden"
    INVALID_UTF8 = "dockerfile.invalid_utf8"
    MISSING = "dockerfile.missing"
    NETWORK_HOST = "dockerfile.network_host"
    ONBUILD_FORBIDDEN = "dockerfile.onbuild_forbidden"
    ROOT_USER = "dockerfile.root_user"
    SECRET_MOUNT = "dockerfile.secret_mount"


class StopPlanCode(WireEnum):
    """Why a stop plan is stale or cannot be taken."""

    CAPACITY_RELEASE_DEFERRED = "stop.capacity_release_deferred"
    RANK_MEMBERSHIP_CHANGED = "stop.rank_membership_changed"
    RESERVATION_MEMBERSHIP_CHANGED = "stop.reservation_membership_changed"
    RUN_NOT_STOPPABLE = "stop.run_not_stoppable"
    TARGET_SCOPE_CHANGED = "stop.target_scope_changed"


class StorageDemandCode(WireEnum):
    """Storage demand outcomes of an admission."""

    EVICTING = "storage.evicting"
    INSUFFICIENT_AFTER_EVICTION = "storage.insufficient_after_eviction"
    EVICTION_TIMED_OUT = "storage.eviction_timed_out"


class SupersedeCode(WireEnum):
    """Why a fleet profile application was superseded by newer intent."""

    SUPERSEDED_BY_INTENT = "superseded-by-intent"
    SUPERSEDED_BY_RETRY = "superseded-by-retry"
    EFFECTS_CHANGED_DURING_ADMISSION = "effects-changed-during-admission"


class TopologyCode(WireEnum):
    """Topology planning refusals."""

    FABRIC_INSUFFICIENT = "topology.fabric_insufficient"
    INVALID = "topology.invalid"
    PLACEMENT_INVALID = "topology.placement_invalid"
    ROLE_MISMATCH = "topology.role_mismatch"
    RUNTIME_CAPABILITY_MISSING = "topology.runtime_capability_missing"


class UninstallPlanCode(WireEnum):
    """Why an uninstall plan is blocked or incomplete."""

    ABANDON_NEVER_INSTALLED = "uninstall.abandon-never-installed"
    ACTIVE_RUN = "uninstall.active_run"
    ACTIVE_RUNS_TRUNCATED = "uninstall.active_runs_truncated"
    BYTES_UNKNOWN = "uninstall.bytes_unknown"
    INSTALLATION_NOT_UNINSTALLABLE = "uninstall.installation_not_uninstallable"
    OPERATION_ACTIVE = "uninstall.operation_active"
    RANK_MEMBERSHIP_CHANGED = "uninstall.rank_membership_changed"


#: Every domain enum, in the order the wire schema lists them.
REASON_CODE_ENUMS: tuple[type[WireEnum], ...] = (
    AdmissionCode,
    AgentEvidenceCode,
    ArtifactLifecycleCode,
    CacheReferenceReason,
    CatalogCode,
    CatalogSyncCode,
    ClusterMappingCode,
    ControllerErrorCode,
    DistributionCode,
    HelperErrorCode,
    ImageStoreCode,
    InstallAdmissionCode,
    InstallDegradedReason,
    LibraryAssessmentCode,
    LibraryProjectionCode,
    ModelCacheBlockerCode,
    ModelCacheCode,
    NodeOfflineReason,
    OperationFailureCode,
    PrebuiltImageCode,
    ProfileReasonCode,
    ProjectionCode,
    RecipeBuildCode,
    RecipeImageCode,
    RecipeOperationCode,
    RecipePackageCode,
    RecipeUpdateCode,
    ReconcileCode,
    ResourcePlanningCode,
    ResourceTerm,
    ResourceTermProblem,
    RunDegradedReason,
    RunSwitchCode,
    RuntimeImageCode,
    RuntimePreflightCode,
    RuntimePreflightFindingCode,
    SourceBundleCode,
    SourcePolicyCode,
    StopPlanCode,
    StorageDemandCode,
    SupersedeCode,
    TopologyCode,
    UninstallPlanCode,
)


@cache
def _index() -> Mapping[str, WireEnum]:
    index: dict[str, WireEnum] = {}
    for enum in REASON_CODE_ENUMS:
        for member in enum:
            index.setdefault(member.value, member)
    return index


def reason_code_of(word: str) -> WireEnum | None:
    """The contract member that spells ``word``, or ``None`` for a word no domain owns.

    A stored or relayed code that this release does not know is never a failure:
    the reader keeps the word as text and shows it, so a newer writer's code
    survives an older reader.
    """

    return _index().get(adopt_reason_code(word))


#: Spellings an older Controller stored that a later one spells differently.  A
#: reader adopts them (:func:`adopt_reason_code`); nothing writes them any more.
RETIRED_CODE_SPELLINGS: Mapping[str, WireEnum] = {
    "run-switch.installation_preparation_unavailable": (
        RunSwitchCode.INSTALLATION_PREPARATION_UNAVAILABLE
    ),
    "run-switch.stop_plan_unavailable": RunSwitchCode.STOP_PLAN_UNAVAILABLE,
}


def adopt_reason_code(stored: str) -> str:
    """The current spelling of a stored code (the word itself when it is current)."""

    adopted = RETIRED_CODE_SPELLINGS.get(stored)
    return stored if adopted is None else str(adopted.value)


#: The prefix every :class:`RuntimePreflightFindingCode` word carries.  An agent
#: that predates the enum sent the bare word (``available``,
#: ``helper_operation_io``) and, for the podman and probe diagnostics, a kebab-case
#: spelling (``proc-mount-denied``).
_FINDING_PREFIX = "preflight_finding."

#: The words an older agent reported as free text, each read as the member that
#: names it.  This is the one legacy adapter for a finding code; nothing writes
#: these spellings any more.  It is scoped to the finding code (not folded into
#: :data:`RETIRED_CODE_SPELLINGS`) because a bare legacy word such as
#: ``helper_grant_invalid`` is also a member of another domain.
RETIRED_FINDING_CODE_SPELLINGS: Mapping[str, RuntimePreflightFindingCode] = {
    **{
        member.value.removeprefix(_FINDING_PREFIX): member
        for member in RuntimePreflightFindingCode
    },
    **{
        word: RuntimePreflightFindingCode(_FINDING_PREFIX + word.replace("-", "_"))
        for word in (
            "build-step-failed",
            "capabilities-not-zero",
            "memory-limit-exceeded",
            "mount-namespace-unavailable",
            "no-new-privileges-unavailable",
            "nonzero-without-output",
            "oci-runtime-unavailable",
            "patch-rejected",
            "permission-denied",
            "proc-mount-denied",
            "proc-unavailable",
            "storage-driver-failure",
            "subordinate-id-mapping-unavailable",
            "systemd-scope-failure",
            "temporary-directory-unavailable",
            "temporary-storage-exhausted",
            "unclassified-podman-build-failure",
            "user-namespace-denied",
            "user-service-manager-unavailable",
        )
    },
}


def adopt_preflight_finding_code(stored: str) -> RuntimePreflightFindingCode:
    """The member that names a finding code read from an agent.

    A current spelling and a retired free-text spelling both map to their member.
    A word this release does not know (a newer agent's, or free text no member
    spells) reads as ``UNCLASSIFIED``: the finding is kept and shown rather than
    refusing the whole preflight result.
    """

    try:
        return RuntimePreflightFindingCode(stored)
    except ValueError:
        return RETIRED_FINDING_CODE_SPELLINGS.get(
            stored, RuntimePreflightFindingCode.UNCLASSIFIED
        )


def run_switch_code(inner: str) -> RunSwitchCode:
    """The Run/Switch code that wraps a code of another domain.

    Run/Switch shows a resource, reconcile, stop or uninstall code as its own
    (``run-switch.`` plus the inner code).  Every inner code of those domains is a
    member; a word no domain owns maps to ``REASON_UNCLASSIFIED`` and the detail
    text carries the cause.
    """

    return _RUN_SWITCH_WRAPPED.get(str(inner), RunSwitchCode.REASON_UNCLASSIFIED)


_RUN_SWITCH_WRAPPED: Mapping[str, RunSwitchCode] = {
    member.value.removeprefix("run-switch."): member for member in RunSwitchCode
}


def resource_term_code(
    term: ResourceTerm, problem: ResourceTermProblem
) -> ResourcePlanningCode:
    """The resource-planning code for one problem with one effective setting."""

    return ResourcePlanningCode(f"resource.{term.value}_{problem.value}")


class ReasonCodeVocabulary(WireModel):
    """Carrier that publishes every reason-code enum into the wire schema.

    The model is never sent: it exists so the schema exporter, the Rust
    generator and the OpenAPI/TypeScript generators emit each closed word set
    from this one module.
    """

    admission_code: AdmissionCode
    agent_evidence_code: AgentEvidenceCode
    artifact_lifecycle_code: ArtifactLifecycleCode
    cache_reference_reason: CacheReferenceReason
    catalog_code: CatalogCode
    catalog_sync_code: CatalogSyncCode
    cluster_mapping_code: ClusterMappingCode
    controller_error_code: ControllerErrorCode
    distribution_code: DistributionCode
    helper_error_code: HelperErrorCode
    image_store_code: ImageStoreCode
    install_admission_code: InstallAdmissionCode
    install_degraded_reason: InstallDegradedReason
    library_assessment_code: LibraryAssessmentCode
    library_projection_code: LibraryProjectionCode
    model_cache_blocker_code: ModelCacheBlockerCode
    model_cache_code: ModelCacheCode
    node_offline_reason: NodeOfflineReason
    operation_failure_code: OperationFailureCode
    prebuilt_image_code: PrebuiltImageCode
    profile_reason_code: ProfileReasonCode
    projection_code: ProjectionCode
    recipe_build_code: RecipeBuildCode
    recipe_image_code: RecipeImageCode
    recipe_operation_code: RecipeOperationCode
    recipe_package_code: RecipePackageCode
    recipe_update_code: RecipeUpdateCode
    reconcile_code: ReconcileCode
    resource_planning_code: ResourcePlanningCode
    resource_term: ResourceTerm
    resource_term_problem: ResourceTermProblem
    run_degraded_reason: RunDegradedReason
    run_switch_code: RunSwitchCode
    runtime_image_code: RuntimeImageCode
    runtime_preflight_code: RuntimePreflightCode
    runtime_preflight_finding_code: RuntimePreflightFindingCode
    source_bundle_code: SourceBundleCode
    source_policy_code: SourcePolicyCode
    stop_plan_code: StopPlanCode
    storage_demand_code: StorageDemandCode
    supersede_code: SupersedeCode
    topology_code: TopologyCode
    uninstall_plan_code: UninstallPlanCode


__all__ = [
    "REASON_CODE_ENUMS",
    "RETIRED_CODE_SPELLINGS",
    "RETIRED_FINDING_CODE_SPELLINGS",
    "AdmissionCode",
    "AgentEvidenceCode",
    "ArtifactLifecycleCode",
    "CacheReferenceReason",
    "CatalogCode",
    "CatalogSyncCode",
    "ClusterMappingCode",
    "ControllerErrorCode",
    "DistributionCode",
    "HelperErrorCode",
    "ImageStoreCode",
    "InstallAdmissionCode",
    "InstallDegradedReason",
    "LibraryAssessmentCode",
    "LibraryProjectionCode",
    "ModelCacheBlockerCode",
    "ModelCacheCode",
    "NodeOfflineReason",
    "OperationFailureCode",
    "PrebuiltImageCode",
    "ProfileReasonCode",
    "ProjectionCode",
    "ReasonCodeVocabulary",
    "RecipeBuildCode",
    "RecipeImageCode",
    "RecipeOperationCode",
    "RecipePackageCode",
    "RecipeUpdateCode",
    "ReconcileCode",
    "ResourcePlanningCode",
    "ResourceTerm",
    "ResourceTermProblem",
    "RunDegradedReason",
    "RunSwitchCode",
    "RuntimeImageCode",
    "RuntimePreflightCode",
    "RuntimePreflightFindingCode",
    "SourceBundleCode",
    "SourcePolicyCode",
    "StopPlanCode",
    "StorageDemandCode",
    "SupersedeCode",
    "TopologyCode",
    "UninstallPlanCode",
    "adopt_preflight_finding_code",
    "adopt_reason_code",
    "reason_code_of",
    "resource_term_code",
    "run_switch_code",
]
