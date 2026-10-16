#![cfg(test)]
//! A job never returns an unknown the Controller would observe without end:
//! a deterministic answer ends it at once, and the net at its one exit turns
//! an unknown with neither a receipt nor a cancellation's bound into a
//! failure.

use super::super::test_support::*;
use super::tests::job_claim;
use super::*;

#[test]
fn an_exit_code_out_of_range_ends_the_job_at_once_with_the_raw_value() {
    let (claim, request) = job_claim();
    let stop_plan = job_run_stop_plan(&claim, &request).unwrap();
    let data = tempdir().unwrap();
    let client = AgentHttpClient::for_http_test("http://127.0.0.1:9/", NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: std::path::Path::new(crate::host_runtime::HELPER_SOCKET),
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
        runtime_root: data.path(),
    };
    let job_scope = request.job_id.to_string();
    let job = DispatchedJob {
        claim: &claim,
        request: &request,
        stop_plan: &stop_plan,
        job_scope: &job_scope,
        started: Instant::now(),
    };
    for (exit_code, raw) in [
        (Some(300), "Some(300)"),
        (Some(-1), "Some(-1)"),
        (None, "None"),
    ] {
        let outcome = crate::host_runtime::HostRuntimeOutcome {
            exit_code,
            stop_uncertain: false,
            diagnostic: None,
            process_logs: None,
        };
        let result = executor
            .job_exit_receipt(&job, outcome)
            .expect_err("no receipt for an exit code out of range");
        let failure = result
            .failure()
            .expect("a definite failure, not an unknown");
        assert_eq!(failure.stage, Some(FailureStage::JobState));
        assert_eq!(
            failure.diagnostic.as_deref(),
            Some(format!("exit_code_raw={raw}").as_str())
        );
    }
}

fn unbounded_unknown() -> ExecutionResult {
    ExecutionResult::unknown(
        WaitReason::JobStateUncertain,
        "job custody or delivery remains unobserved",
        UnknownEvidence::at(FailureStage::RuntimeAdapter).because("helper answer lost"),
    )
}

#[test]
fn the_net_ends_an_unknown_without_receipt_or_bound_and_keeps_bounded_ones() {
    // Neither a receipt nor a cancellation's bound: failed, with its evidence.
    let ended = bounded_job_result(unbounded_unknown());
    let failure = ended.failure().expect("an unbounded unknown ends failed");
    assert_eq!(failure.stage, Some(FailureStage::RuntimeAdapter));
    let diagnostic = failure.diagnostic.as_deref().unwrap_or_default();
    assert!(
        diagnostic.contains("unknown without receipt or bound")
            && diagnostic.contains("helper answer lost"),
        "{diagnostic}"
    );

    // A receipt bounds it: the Controller ends the order with that exit.
    let (_, request) = job_claim();
    let ExecutionResult::Unknown(mut receipted) = unbounded_unknown() else {
        unreachable!()
    };
    receipted.receipt = Some(job_receipt(
        &request,
        0,
        Instant::now(),
        empty_job_output_manifest(),
        None,
    ));
    assert!(matches!(
        bounded_job_result(ExecutionResult::Unknown(receipted)),
        ExecutionResult::Unknown(_)
    ));

    // A cancellation's unconfirmed stop is bounded by the cancel.
    let ExecutionResult::Unknown(mut cancelling) = unbounded_unknown() else {
        unreachable!()
    };
    cancelling.wait_reason = WaitReason::JobStopUnconfirmed;
    assert!(matches!(
        bounded_job_result(ExecutionResult::Unknown(cancelling)),
        ExecutionResult::Unknown(_)
    ));
}

#[test]
fn a_receipted_unknown_is_the_shared_vector_on_the_wire() {
    // The agent reports a receipted ending's stage and cause as the outcome's
    // evidence, beside the receipt; the Controller stores them with it.
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-unknown-receipt-v1.json"
    ))
    .unwrap();
    let (claim, request) = job_claim();
    let stop_plan = job_run_stop_plan(&claim, &request).unwrap();
    let receipt: vonk_agent_protocol::RecipeJobRunResult =
        serde_json::from_value(vector["result"]["receipt"].clone()).unwrap();
    let job_scope = request.job_id.to_string();
    let job = DispatchedJob {
        claim: &claim,
        request: &request,
        stop_plan: &stop_plan,
        job_scope: &job_scope,
        started: Instant::now(),
    };
    let finished = job
        .unknown_after_exit(
            &receipt,
            FailureStage::JobState,
            "job output delivery remains unobserved",
            "artifact output upload was refused: HTTP 409".to_owned(),
        )
        .finish(&claim);
    assert_eq!(
        serde_json::to_value(finished.state).unwrap(),
        vector["state"]
    );
    assert_eq!(
        serde_json::to_value(&finished.result).unwrap(),
        vector["result"]
    );
    assert_eq!(claim.fence.to_string(), vector["fence"]);
}
