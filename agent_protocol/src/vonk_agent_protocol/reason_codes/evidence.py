"""Evidence reason codes."""

from ..wire_model import WireEnum


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
    STORED_RESULT_UNREADABLE = "stored_operation_result_unreadable"


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
