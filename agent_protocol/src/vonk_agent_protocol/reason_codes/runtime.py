"""Runtime reason codes."""

from ..wire_model import WireEnum


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
