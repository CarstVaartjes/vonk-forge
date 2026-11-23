use super::{
    HOST_RUNTIME_REQUEST_ENVELOPE_BYTES, HostRuntimeAction, HostRuntimeRequest,
    HostRuntimeRequestRule, MAX_HOST_RUNTIME_REQUEST_BYTES, canonical_json,
};
use uuid::Uuid;

fn start(arguments: Vec<String>) -> HostRuntimeRequest {
    let plan = super::recipe_start_tests::valid_start_plan();
    HostRuntimeRequest {
        inspect_target: None,
        action: HostRuntimeAction::Start,
        fence: Uuid::new_v4(),
        arguments,
        job_plan: None,
        installation_id: None,
        reconciliation_identity: None,
        run_generation: Some(plan.run_generation),
        start_plan: Some(plan),
        stop_plan: None,
    }
}

fn canonical_length(request: &HostRuntimeRequest) -> usize {
    canonical_json(request)
        .expect("a host runtime request must canonically encode")
        .len()
}

#[test]
fn the_declared_envelope_covers_the_largest_contract_permitted_request() {
    // Wrong implementation: `MAX_ARGV_BYTES` was set equal to the request
    // ceiling while its comment called it a backstop "below" that ceiling,
    // so an argv inside the plan's own budget could still be unframeable.
    // Re-measure the envelope rather than restating the subtraction.
    let request = HostRuntimeRequest {
        inspect_target: None,
        action: HostRuntimeAction::RunInspect,
        fence: Uuid::new_v4(),
        arguments: Vec::new(),
        job_plan: None,
        installation_id: None,
        reconciliation_identity: None,
        run_generation: None,
        start_plan: None,
        stop_plan: None,
    };
    let measured = canonical_length(&request);
    assert!(
        measured <= HOST_RUNTIME_REQUEST_ENVELOPE_BYTES,
        "the measured envelope {measured} outgrew the declared \
             HOST_RUNTIME_REQUEST_ENVELOPE_BYTES {HOST_RUNTIME_REQUEST_ENVELOPE_BYTES}, \
             so the derived MAX_ARGV_BYTES no longer leaves room for the request around it"
    );
}

#[test]
fn the_exchange_ceiling_is_admitted_and_one_byte_over_is_refused_with_both_numbers() {
    // Wrong implementation: the request had no byte bound at all, so the
    // helper's private `64 * 1024` read cap refused it opaquely as
    // `helper.unsafe_path` after the agent had called it valid.
    let base = start(vec!["sha256:image".to_owned()]);
    let base_length = canonical_length(&base);
    let filler = MAX_HOST_RUNTIME_REQUEST_BYTES - base_length - 3;

    let mut at_limit = base.clone();
    at_limit.arguments.push("x".repeat(filler));
    assert_eq!(canonical_length(&at_limit), MAX_HOST_RUNTIME_REQUEST_BYTES);
    assert_eq!(at_limit.validate(), Ok(()));

    let mut over_limit = base;
    over_limit.arguments.push("x".repeat(filler + 1));
    assert_eq!(
        canonical_length(&over_limit),
        MAX_HOST_RUNTIME_REQUEST_BYTES + 1
    );
    assert_eq!(
        over_limit.validate(),
        Err(HostRuntimeRequestRule::RequestBytes {
            limit: MAX_HOST_RUNTIME_REQUEST_BYTES as u64,
            observed: MAX_HOST_RUNTIME_REQUEST_BYTES as u64 + 1,
        })
    );
}

#[test]
fn a_command_line_above_the_retired_count_ceiling_is_admitted() {
    // Wrong implementation: `MAX_HOST_RUNTIME_ARGUMENTS = 4096` refused a
    // legitimate command line of 6000 short elements while the canonical
    // request was tens of kilobytes inside the byte budget. The plan
    // projects two arguments per mounted file at an artifact ceiling of
    // 4096, so a count this large is admitted by the plan contract.
    let arguments = (0..6000)
        .map(|index| format!("--mount=type=bind,src={index:05}"))
        .collect();
    let request = start(arguments);
    assert!(canonical_length(&request) < MAX_HOST_RUNTIME_REQUEST_BYTES);
    assert_eq!(request.validate(), Ok(()));
}

#[test]
fn an_empty_argument_and_a_multiline_argument_are_legal_bytes() {
    // Wrong implementation: an argument an exec argv can carry -- empty, or
    // one with CR/LF -- was refused for carrying it, though the plan's own
    // opaque-argv contract permits it byte for byte.
    let request = start(vec![
        "sha256:image".to_owned(),
        String::new(),
        "line one\nline two\r\n".to_owned(),
        String::new(),
    ]);
    assert_eq!(request.validate(), Ok(()));
}
