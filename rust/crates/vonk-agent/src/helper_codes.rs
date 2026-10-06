//! The privileged helper's error codes, and which of them each reader accepts.
//!
//! The vocabulary is one generated enum, `HelperErrorCode`, in the shared
//! contract: the helper builds a rejection from a member and the agent reads a
//! reply through the enum, so a code outside it is a malformed reply and never
//! a code the agent forwards.  What differs per reader is only the subset it
//! trusts, and that is kept here as matches over members.

use vonk_agent_protocol::generated::HelperErrorCode as Code;

/// The code a helper reply to a privileged runtime call may name.
pub fn runtime_rejection(value: &str) -> Option<Code> {
    value
        .parse()
        .ok()
        .filter(|code| is_runtime_rejection(*code))
}

fn is_runtime_rejection(code: Code) -> bool {
    matches!(
        code,
        Code::GrantInvalid
            | Code::GrantNodeMismatch
            | Code::GrantUnauthorized
            | Code::PeerIdentityInvalid
            | Code::OperationInvalidArtifact
            | Code::RuntimeImageIdentityInvalid
            | Code::RequestReplayed
            | Code::OperationFailed
            | Code::InstallationReconciliationBusy
            | Code::OperationInvalid
            | Code::OperationUnsafePath
            | Code::OperationCommandFailed
            | Code::OperationStopUncertain
            | Code::OperationIo
            | Code::RuntimeImageLoadFailed
            | Code::RuntimeImageInspectFailed
            | Code::RuntimeImageReceiptFailed
            | Code::RuntimeProcessExited
            | Code::RuntimeRunMissing
            | Code::RuntimeFabricUnavailable
            | Code::RuntimeFabricFirewallRejected
            | Code::RuntimeEndpointFirewallRejected
            | Code::InstallationReconciliationStorageUnavailable
            | Code::RequestInvalid
            | Code::RequestLedgerFailed
            | Code::RequestEncodingInvalid
            | Code::CallJoinFailed
            | Code::MessageFramingInvalid
            | Code::ResponseUnbound
            | Code::RejectionMalformed
            | Code::OutcomeMalformed
            | Code::RequestDocumentInvalid
            | Code::RequestSchemaVersionInvalid
            | Code::RequestAttemptInvalid
            | Code::RequestArgumentsPresenceInvalid
            | Code::RequestPlanBindingInvalid
            | Code::RequestInstallationIdentityInvalid
            | Code::RequestBytesInvalid
            | Code::RequestPlanBytesInvalid
            | Code::RequestArgumentNulByte
            | Code::RequestStorageInvalid
            | Code::SystemClockInvalid
            | Code::InspectionOutcomeInvalid
    )
}

/// The refusals a helper raises before it has trusted the grant: the only
/// rejections that legitimately arrive without the request identity (with the
/// malformed-request refusal).
pub fn is_unbound_rejection(code: Code) -> bool {
    matches!(
        code,
        Code::GrantInvalid
            | Code::GrantNodeMismatch
            | Code::GrantUnauthorized
            | Code::PeerIdentityInvalid
            | Code::RequestInvalid
    )
}

/// The code a reply to the agent upgrade's helper call may name.
pub fn upgrade_rejection(value: &str) -> Option<Code> {
    value.parse().ok().filter(|code| {
        matches!(
            code,
            Code::GrantInvalid
                | Code::GrantNodeMismatch
                | Code::GrantUnauthorized
                | Code::PeerIdentityInvalid
                | Code::RequestReplayed
                | Code::OperationFailed
                | Code::RequestInvalid
                | Code::RequestLedgerFailed
                | Code::PackagePreflightFailed
                | Code::PackageVerificationFailed
                | Code::PackageMetadataFailed
                | Code::PackageCustodyFailed
                | Code::PackageInstallFailed
                | Code::ConcurrencyLimit
        )
    })
}

/// Whether a helper code in a failed distribution operation's evidence crosses
/// the wire.
pub fn is_distribution_evidence(code: Code) -> bool {
    matches!(
        code,
        Code::GrantInvalid
            | Code::GrantNodeMismatch
            | Code::GrantUnauthorized
            | Code::PeerIdentityInvalid
            | Code::RequestReplayed
            | Code::OperationFailed
            | Code::OperationInvalidArtifact
            | Code::RuntimeImageIdentityInvalid
            | Code::OperationInvalid
            | Code::OperationUnsafePath
            | Code::OperationCommandFailed
            | Code::OperationStopUncertain
            | Code::OperationIo
            | Code::RuntimeImageLoadFailed
            | Code::RuntimeImageInspectFailed
            | Code::RuntimeImageReceiptFailed
            | Code::RuntimeHelperUnavailable
            | Code::RuntimeAuthorityUnavailable
            | Code::RuntimeHelperProtocolInvalid
            | Code::RequestInvalid
            | Code::RequestLedgerFailed
            | Code::RuntimeHelperStopUncertain
            | Code::RuntimeHelperRequestEncodingInvalid
            | Code::RuntimeHelperCallJoinFailed
            | Code::RuntimeHelperMessageFramingInvalid
            | Code::RuntimeHelperResponseUnbound
            | Code::RuntimeHelperRejectionMalformed
            | Code::RuntimeHelperOutcomeMalformed
            | Code::RuntimeHelperRequestDocumentInvalid
            | Code::RuntimeHelperRequestSchemaVersionInvalid
            | Code::RuntimeHelperRequestAttemptInvalid
            | Code::RuntimeHelperRequestArgumentsPresenceInvalid
            | Code::RuntimeHelperRequestPlanBindingInvalid
            | Code::RuntimeHelperRequestInstallationIdentityInvalid
            | Code::RuntimeHelperRequestBytesInvalid
            | Code::RuntimeHelperRequestPlanBytesInvalid
            | Code::RuntimeHelperRequestArgumentNulByte
            | Code::RuntimeHelperRequestStorageInvalid
            | Code::RuntimeHelperSystemClockInvalid
            | Code::RuntimeHelperInspectionOutcomeInvalid
    )
}

/// Whether a helper code in a failed agent upgrade's evidence crosses the wire.
pub fn is_upgrade_evidence(code: Code) -> bool {
    matches!(
        code,
        Code::PackageVerificationFailed
            | Code::PackageMetadataFailed
            | Code::PackageCustodyFailed
            | Code::PackageInstallFailed
    )
}
