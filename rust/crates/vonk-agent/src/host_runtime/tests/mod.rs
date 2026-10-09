#![cfg(test)]

use super::{
    HelperErrorCode, HelperProtocolCause, HostRuntimeError, call_helper, require_bound_response,
    require_executed_outcome, write_request,
};
use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::{PermissionsExt, symlink};
use std::os::unix::net::UnixListener;
use std::path::Path;
use std::time::Duration;
use uuid::Uuid;
use vonk_agent_protocol::{HostRuntimeAction, RecipeStartRequest};

fn valid_executed_response() -> super::HelperResponse {
    super::HelperResponse {
        schema_version: 1, request_id: Some(Uuid::new_v4()),
        status: vonk_agent_protocol::generated::HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
        error_code: None, diagnostic: None, process_logs: None, exit_code: Some(0), process_running: None,
    }
}

fn assert_rejection_malformed(response: &super::HelperResponse, _action: HostRuntimeAction) {
    assert!(require_executed_outcome(response, false).is_err());
    require_executed_outcome(&valid_executed_response(), false).unwrap();
}

fn assert_outcome_malformed(response: &super::HelperResponse) {
    assert!(require_executed_outcome(response, false).is_err());
    require_executed_outcome(&valid_executed_response(), false).unwrap();
}

/// The exact Start request shape `execute_bound` sends, so each rule test
/// drives the canonical validator instead of asserting a bare constant.
fn start_plan() -> RecipeStartRequest {
    let mut compiled: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    compiled["runtime"]["placement"]["endpoint_address"] = serde_json::json!("100.100.20.30");
    compiled["security"]["network_mode"] = serde_json::json!("bridge");
    serde_json::from_value(serde_json::json!({
        "run_id": "00000000-0000-4000-8000-000000000003",
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "recipe_revision_id": "00000000-0000-4000-8000-000000000002",
        "mapping_id": "00000000-0000-4000-8000-000000000007",
        "plan_digest": "c".repeat(64),
        "compiled_execution_plan": compiled,
        "run_generation": 1
    }))
    .unwrap()
}

fn start_request() -> super::HostRuntimeRequest {
    let plan = start_plan();
    super::HostRuntimeRequest {
        action: HostRuntimeAction::Start,
        fence: Uuid::new_v4(),
        arguments: vec!["sha256:image".to_owned(), "run".to_owned()],
        job_plan: None,
        installation_id: None,
        reconciliation_identity: None,
        run_generation: Some(plan.run_generation),
        start_plan: Some(plan),
        stop_plan: None,
    }
}

fn request_rule_code(request: &super::HostRuntimeRequest) -> String {
    let rule = request
        .validate()
        .expect_err("this request must violate the rule under test");
    let error = HostRuntimeError::request_refusal(rule);
    assert!(error.diagnostic().is_none());
    error.preflight_code()
}

fn request_at_bytes(target: usize) -> super::HostRuntimeRequest {
    let mut request = start_request();
    let base = vonk_agent_protocol::canonical_json(&request)
        .expect("a start request canonically encodes")
        .len();
    request.arguments.push("x".repeat(target - base - 3));
    request
}

#[test]
fn request_root_publication_has_no_permissive_intermediate_state() {
    const CHILD: &str = "VONK_REQUEST_ROOT_PUBLICATION_COUNTERPROOF";
    if std::env::var_os(CHILD).is_none() {
        // Umask is process-wide. Give only this isolated child the old
        // publisher's ordinary 022 umask; parallel tests remain untouched.
        let mut child = std::process::Command::new("sh")
            .args(["-c", "umask 022; exec \"$@\"", "request-root-counterproof"])
            .arg(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "host_runtime::tests::request_root_publication_has_no_permissive_intermediate_state",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .spawn()
            .unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(30);
        // Reserve one second inside the existing total budget to reap
        // this exact child; killing is not itself proof of completion.
        let work_deadline = deadline - Duration::from_secs(1);
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert!(
                    status.success(),
                    "request root publication counterproof failed"
                );
                return;
            }
            if std::time::Instant::now() >= work_deadline {
                let kill_error = child.kill().err();
                while std::time::Instant::now() < deadline {
                    if child.try_wait().unwrap().is_some() {
                        panic!(
                            "request root publication counterproof exceeded its elapsed budget; exact child reaped"
                        );
                    }
                    std::thread::sleep(Duration::from_millis(1));
                }
                panic!(
                    "request root publication child {} remains unreaped after its elapsed budget; kill error: {:?}",
                    child.id(),
                    kill_error
                );
            }
            std::thread::sleep(Duration::from_millis(1));
        }
    }
    let deadline = std::time::Instant::now() + Duration::from_secs(30);
    let temp = tempfile::tempdir().unwrap();
    let old_root = temp.path().join("two-phase");
    // Pause the old real two-phase publisher after mkdir, before chmod.
    // Another actual writer must refuse the published unsafe root.
    fs::create_dir(&old_root).unwrap();
    assert_eq!(
        fs::metadata(&old_root).unwrap().permissions().mode() & 0o777,
        0o755
    );
    let (result_sender, result_receiver) = std::sync::mpsc::channel();
    let writer_root = old_root.clone();
    let writer = std::thread::spawn(move || {
        let result = write_request(&writer_root, &"a".repeat(64), b"{}");
        let _ = result_sender.send(result);
    });
    let result = match result_receiver
        .recv_timeout(deadline.saturating_duration_since(std::time::Instant::now()))
    {
        Ok(result) => result,
        Err(error) => {
            let retained = temp.keep();
            panic!(
                "request root writer outcome unresolved ({error}); owned fixture retained at {}",
                retained.display()
            );
        }
    };
    while !writer.is_finished() && std::time::Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(1));
    }
    if !writer.is_finished() {
        let retained = temp.keep();
        panic!(
            "request root writer completion unresolved; owned fixture retained at {}",
            retained.display()
        );
    }
    // The same owned handle was observed finished. Joining only surfaces
    // its panic; it cannot wait for further writer work or fixture cleanup.
    writer.join().unwrap();
    assert!(result.is_ok());
    fs::set_permissions(&old_root, fs::Permissions::from_mode(0o700)).unwrap();
    assert!(write_request(&old_root, &"a".repeat(64), b"{}").is_ok());

    let atomic_root = temp.path().join("atomic");
    let path = write_request(&atomic_root, &"b".repeat(64), b"{}").unwrap();
    assert_eq!(
        fs::metadata(&atomic_root).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(fs::read(path).unwrap(), b"{}");
    // The cancellation proof below starts all eight native calls against
    // one absent root, and keeps their actual files through cancellation.
}

mod observation;
mod requests;
mod responses;
