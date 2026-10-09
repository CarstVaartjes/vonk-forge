"""Admission reason codes."""

from ..wire_model import WireEnum


class AdmissionCode(WireEnum):
    """The shared admission lock refused because capacity is held by another admission."""

    CAPACITY_BUSY = "admission.capacity_busy"


class CertificateCode(WireEnum):
    """Owned certificate issuance admission refusals."""

    RESPONSE_UNREPRESENTABLE = "certificate.response_unrepresentable"
    REQUEST_INVALID = "certificate.request_invalid"
    AUTHENTICATION_REFUSED = "certificate.authentication_refused"
    BINDING_REFUSED = "certificate.binding_refused"
    SOURCE_REVOKED = "certificate.source_revoked"
    SOURCE_IDENTITY_REFUSED = "certificate.source_identity_refused"
    ISSUANCE_IN_PROGRESS = "certificate.issuance_in_progress"
    ISSUANCE_UNAVAILABLE = "certificate.issuance_unavailable"
    REQUEST_BINDING_MISMATCH = "certificate.request_binding_mismatch"
    SERIAL_ALREADY_RESERVED = "certificate.serial_already_reserved"
    SERIAL_ALREADY_ISSUED = "certificate.serial_already_issued"
    ATTEMPT_SUPERSEDED = "certificate.attempt_superseded"
    ISSUANCE_REVOKED = "certificate.issuance_revoked"
    ROTATION_SOURCE_REVOKED = "certificate.rotation_source_revoked"


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
    SUPERSEDED_OPERATION_CANCELLED = "superseded_operation_cancelled"
    TIMEOUT = "controller.timeout"
    UNAVAILABLE = "controller.unavailable"


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


class UninstallPlanCode(WireEnum):
    """Why an uninstall plan is blocked or incomplete."""

    ABANDON_NEVER_INSTALLED = "uninstall.abandon-never-installed"
    ACTIVE_RUN = "uninstall.active_run"
    ACTIVE_RUNS_TRUNCATED = "uninstall.active_runs_truncated"
    BYTES_UNKNOWN = "uninstall.bytes_unknown"
    INSTALLATION_NOT_UNINSTALLABLE = "uninstall.installation_not_uninstallable"
    OPERATION_ACTIVE = "uninstall.operation_active"
    RANK_MEMBERSHIP_CHANGED = "uninstall.rank_membership_changed"
