use super::*;
use vonk_agent_protocol::generated::RecipeStopResult;

#[test]
fn only_a_stop_that_established_its_effect_is_named() {
    // Wrong implementation: a cancelled, failed or unconfirmed Stop (or any
    // other order) named as completed lets a pre-stop reading admit a load.
    let mut claim: AgentClaim = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    let stops = CompletedStops::default();
    let done = ExecutionResult::done(RecipeStopResult::default());
    stops.observe(&claim, &done);
    claim.operation = AgentOperation::RecipeStop;
    for ended in [
        ExecutionResult::cancelled("the order was withdrawn"),
        ExecutionResult::failed("the helper refused the stop"),
    ] {
        stops.observe(&claim, &ended);
    }
    assert_eq!(stops.snapshot(), (0, vec![]));
    stops.observe(&claim, &done);
    assert_eq!(stops.snapshot(), (1, vec![claim.fence]));
}
