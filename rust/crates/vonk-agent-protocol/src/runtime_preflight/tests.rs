#![cfg(test)]
use crate::{HostRuntimeAction, HostRuntimeRequest};
#[test]
fn fixed_helper_preflight_cannot_carry_a_command_or_mount_argument() {
    let mut request = HostRuntimeRequest {
        inspect_target: None,
        action: HostRuntimeAction::RuntimePreflight,
        fence: uuid::Uuid::new_v4(),
        arguments: vec![],
        job_plan: None,
        installation_id: None,
        reconciliation_identity: None,
        run_generation: None,
        start_plan: None,
        stop_plan: None,
    };
    assert!(request.validate().is_ok());
    request.arguments = vec!["--privileged".into()];
    assert!(request.validate().is_err());
    request.arguments.clear();
    request.action = HostRuntimeAction::Start;
    assert!(request.validate().is_err());
}
