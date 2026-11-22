//! Robustness: the same bytes through every Controller-to-agent and
//! agent-to-helper parser never panic (a release panic is an abort, so a crash
//! here is a restart loop). The invariant is the absence of a panic only.
#![no_main]

use libfuzzer_sys::fuzz_target;
use vonk_agent::{client::parse_claim_response, executor::parse_compiled_execution_plan};
use vonk_agent_helper::protocol::{parse_inspection_request, parse_request, read_frame};

fuzz_target!(|data: &[u8]| {
    let _ = parse_claim_response(200, data);
    let _ = parse_compiled_execution_plan(data);
    let _ = parse_request(data);
    let _ = parse_inspection_request(data);
    let _ = read_frame(&mut &*data);
});
