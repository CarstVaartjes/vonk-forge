#![cfg(test)]
use super::*;

fn identity() -> RecipeReconciliationIdentity {
    RecipeReconciliationIdentity {
        installation_id: Uuid::new_v4(),
        plan_digest: "b".repeat(64),
    }
}

fn claims(identity: RecipeReconciliationIdentity) -> HostHelperGrantClaims {
    HostHelperGrantClaims {
        schema_version: 1,
        authority: HOST_HELPER_AUTHORITY.to_owned(),
        request_id: Uuid::new_v4(),
        node_id: "spk_11111111111111111111111111111111".to_owned(),
        issued_at: 2_100_000_000,
        expires_at: 2_100_000_060,
        operation: HostHelperOperation::ExecuteContainerRuntimeRequestOperation(
            generated::ExecuteContainerRuntimeRequestOperation {
                type_: "execute-container-runtime-request".into(),
                installation_intent_nonce: None,
                installation_intent_ordinal: Some(1),
                action: HostHelperContainerRuntimeAction::InstallationCleanup,
                fence: Uuid::new_v4(),
                request_sha256: "e".repeat(64),
                installation_id: Some(identity.installation_id),
                reconciliation_identity: Some(identity),
                run_generation: None,
                runtime_installation_id: None,
                runtime_run_id: None,
                runtime_target_id: None,
                start_plan_sha256: None,
                stop_plan_sha256: None,
            },
        ),
    }
}

#[test]
fn signed_cleanup_grant_identity_is_strictly_bound_to_action_and_install() {
    let exact = claims(identity());
    exact.validate().unwrap();

    let mut wrong_installation = exact.clone();
    let HostHelperOperation::ExecuteContainerRuntimeRequestOperation(operation) =
        &mut wrong_installation.operation
    else {
        unreachable!();
    };
    operation.installation_id = Some(Uuid::from_u128(8));
    assert!(wrong_installation.validate().is_err());

    let mut wrong_action = exact;
    let HostHelperOperation::ExecuteContainerRuntimeRequestOperation(operation) =
        &mut wrong_action.operation
    else {
        unreachable!();
    };
    operation.action = HostHelperContainerRuntimeAction::Start;
    operation.installation_id = None;
    assert!(wrong_action.validate().is_err());
}
