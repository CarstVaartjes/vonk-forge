use super::*;

fn request(action: HostRuntimeAction, installation_id: Option<Uuid>) -> HostRuntimeRequest {
    HostRuntimeRequest {
        inspect_target: None,
        action,
        fence: Uuid::new_v4(),
        arguments: Vec::new(),
        job_plan: None,
        installation_id,
        reconciliation_identity: None,
        run_generation: None,
        start_plan: None,
        stop_plan: None,
    }
}

#[test]
fn cleanup_requires_one_installation_identity_and_other_actions_reject_it() {
    let installation_id = Uuid::new_v4();
    assert!(
        request(
            HostRuntimeAction::InstallationCleanup,
            Some(installation_id)
        )
        .validate()
        .is_ok()
    );
    assert!(
        request(HostRuntimeAction::InstallationCleanup, None)
            .validate()
            .is_err()
    );
    let mut ordinary = request(HostRuntimeAction::Start, Some(installation_id));
    ordinary.arguments.push("run".to_owned());
    assert!(ordinary.validate().is_err());

    for raw in [
        serde_json::json!({
            "action": "installation-cleanup",
            "fence": Uuid::new_v4(),
            "arguments": [],
        }),
        serde_json::json!({
            "action": "installation-cleanup",
            "fence": Uuid::new_v4(),
            "arguments": [],
            "installation_id": null,
        }),
    ] {
        let parsed: HostRuntimeRequest = serde_json::from_value(raw).unwrap();
        assert!(parsed.validate().is_err());
    }
}
