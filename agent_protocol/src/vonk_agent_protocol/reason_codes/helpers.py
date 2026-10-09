"""Helpers reason codes."""

from ..wire_model import WireEnum


class HelperOperationCode(WireEnum):
    """Stable diagnostic codes returned by the privileged operation executor."""

    ARTIFACT_INVALID = "helper.artifact_invalid"
    COMMAND_FAILED = "helper.command_failed"
    INSTALLATION_RECONCILIATION_BUSY = "helper.installation_reconciliation_busy"
    INSTALLATION_RECONCILIATION_STORAGE_UNAVAILABLE = (
        "helper.installation_reconciliation_storage_unavailable"
    )
    INSTALLATION_INTENT_OBSERVATION_REQUIRED = (
        "helper.installation_intent_observation_required"
    )
    IO_FAILED = "helper.io_failed"
    OPERATION_INVALID = "helper.operation_invalid"
    PACKAGE_INSTALL_FAILED = "helper.package_install_failed"
    PACKAGE_METADATA_INVALID = "helper.package_metadata_invalid"
    PACKAGE_PREPARATION_UNAVAILABLE = "helper.package_preparation_unavailable"
    PACKAGE_PREFLIGHT_FAILED = "helper.package_preflight_failed"
    RUNTIME_ENDPOINT_FIREWALL_REJECTED = "helper.runtime_endpoint_firewall_rejected"
    RUNTIME_FABRIC_FIREWALL_REJECTED = "helper.runtime_fabric_firewall_rejected"
    RUNTIME_FABRIC_UNAVAILABLE = "helper.runtime_fabric_unavailable"
    RUNTIME_IMAGE_IDENTITY_INVALID = "helper.runtime_image_identity_invalid"
    RUNTIME_IMAGE_INSPECT_FAILED = "helper.runtime_image_inspect_failed"
    RUNTIME_IMAGE_LOAD_FAILED = "helper.runtime_image_load_failed"
    RUNTIME_IMAGE_RECEIPT_FAILED = "helper.runtime_image_receipt_failed"
    RUNTIME_INVOCATION_LIMIT_EXCEEDED = "helper.runtime_invocation_limit_exceeded"
    RUNTIME_INVOCATION_LIMITS_UNAVAILABLE = (
        "helper.runtime_invocation_limits_unavailable"
    )
    RUNTIME_INVOCATION_STRING_LIMIT_EXCEEDED = (
        "helper.runtime_invocation_string_limit_exceeded"
    )
    RUNTIME_PROCESS_EXITED = "helper.runtime_process_exited"
    RUNTIME_RUN_MISSING = "helper.runtime_run_missing"
    STOP_UNCERTAIN = "helper.stop_uncertain"
    UNSAFE_PATH = "helper.unsafe_path"


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
    INSTALLATION_INTENT_OBSERVATION_REQUIRED = (
        "installation_intent_observation_required"
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
    PACKAGE_PREPARATION_UNAVAILABLE = "package_preparation_unavailable"
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
