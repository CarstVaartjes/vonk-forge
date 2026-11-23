#![cfg(test)]
use super::*;

#[test]
fn reconciliation_names_one_install_and_its_plan() {
    let payload = serde_json::json!({
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "plan_digest": "b".repeat(64),
    });
    let claim = AgentClaim {
        observation_budget_seconds: 3600,
        deadline: "2026-09-01T12:00:00+00:00".parse().unwrap(),
        fence: Uuid::new_v4(),
        operation: generated::AgentOperation::RecipeReconcile,
        payload: serde_json::from_value(payload).unwrap(),
    };
    let parsed = RecipeOperationRequest::parse(&claim).unwrap();
    assert!(matches!(parsed, RecipeOperationRequest::Reconcile(_)));
}
