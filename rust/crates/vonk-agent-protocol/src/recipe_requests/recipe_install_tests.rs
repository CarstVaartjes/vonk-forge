use super::*;

#[test]
fn schema_two_install_requires_the_inline_compiled_plan() {
    let payload = serde_json::json!({
        "compiled_execution_plan": serde_json::from_str::<Value>(include_str!("../../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json")).unwrap(),
        "expected_bytes": 1024,
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "plan_digest": "a".repeat(64),
    });
    let payload: generated::AgentClaimPayload = serde_json::from_value(payload).unwrap();
    let claim = AgentClaim {
        observation_budget_seconds: 3600,
        deadline: "2026-09-01T12:00:00+00:00".parse().unwrap(),
        fence: Uuid::new_v4(),
        operation: generated::AgentOperation::RecipeInstall,
        payload,
    };
    let RecipeOperationRequest::Install(request) =
        RecipeOperationRequest::parse(&claim).expect("schema 2 install wire should parse")
    else {
        panic!("expected install request");
    };
    let _ = request;
}
