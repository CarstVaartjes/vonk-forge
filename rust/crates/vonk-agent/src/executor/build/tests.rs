#![cfg(test)]

use super::*;
use crate::process::{ProcessError, ProcessOutput, ProcessRunner, Program};
use crate::recipe_builder::cleanup_build_storage;
use std::os::unix::fs::PermissionsExt;
use vonk_agent_protocol::generated::{AgentOperation, AgentResultResult};

/// A Spark where the build's unit and proxy container may still be present.
struct Spark {
    unit_running: bool,
    proxy_container: Option<String>,
}

impl ProcessRunner for Spark {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        _timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        let has = |word: &str| arguments.iter().any(|value| value == word);
        let stdout = match program {
            Program::Systemctl if has("list-units") && self.unit_running => {
                let unit = arguments.last().unwrap();
                if unit.starts_with("vonk-recipe-build-") && !unit.ends_with("-e.service") {
                    format!("{unit} loaded active running fixture\n")
                } else {
                    String::new()
                }
            }
            Program::Systemctl if has("show") => {
                "LoadState=loaded\nActiveState=active\nMainPID=4242\nControlGroup=/fixture\n".into()
            }
            Program::SystemdRun if has("ps") => self
                .proxy_container
                .clone()
                .map(|name| format!("{name}\n"))
                .unwrap_or_default(),
            _ => String::new(),
        };
        Ok(ProcessOutput {
            success: true,
            stdout: stdout.into_bytes(),
            stderr: vec![],
        })
    }
}

struct Staging {
    data: tempfile::TempDir,
    runtime: tempfile::TempDir,
    request: vonk_agent_protocol::RecipeBuildCleanupRequest,
    entry: std::path::PathBuf,
}

fn staging() -> Staging {
    let data = tempfile::tempdir().unwrap();
    let runtime = tempfile::tempdir().unwrap();
    let request = vonk_agent_protocol::RecipeBuildCleanupRequest {
        build_id: uuid::Uuid::new_v4(),
        operation_id: uuid::Uuid::new_v4(),
    };
    let entry =
        data.path()
            .join("build-staging")
            .join(format!("{}-{}", "a".repeat(64), request.build_id));
    std::fs::create_dir_all(entry.join("podman-storage")).unwrap();
    Staging {
        data,
        runtime,
        request,
        entry,
    }
}

fn cleanup(spark: &Spark, staging: &Staging) -> AgentResultResult {
    let result = match cleanup_build_storage(
        spark,
        staging.data.path(),
        staging.runtime.path(),
        &staging.request,
    ) {
        Ok(evidence) => ExecutionResult::done(evidence),
        Err(error) => build_cleanup_unconfirmed(&error),
    };
    result
        .finish_for(&AgentOperation::RecipeBuildCleanupV1)
        .result
}

/// The wire reason and diagnostic of an unconfirmed cleanup.
fn waiting(result: AgentResultResult) -> (String, String) {
    let AgentResultResult::OutcomeUnknown(unknown) = result else {
        panic!("expected an unconfirmed cleanup, got {result:?}");
    };
    assert_eq!(unknown.wait_reason, WaitReason::CleanupUnconfirmed);
    (
        unknown.reason,
        unknown.evidence.unwrap().diagnostic.unwrap(),
    )
}

#[test]
fn a_clean_removal_confirms_the_cleanup() {
    let staging = staging();
    let spark = Spark {
        unit_running: false,
        proxy_container: None,
    };
    assert!(matches!(
        cleanup(&spark, &staging),
        AgentResultResult::OutcomeDone(_)
    ));
    assert!(!staging.entry.exists());
}

#[test]
fn a_still_running_build_unit_is_named_with_what_it_shows() {
    let staging = staging();
    let spark = Spark {
        unit_running: true,
        proxy_container: None,
    };
    let (reason, diagnostic) = waiting(cleanup(&spark, &staging));
    let unit = format!("vonk-recipe-build-{}.service", staging.request.build_id);
    assert!(
        reason.starts_with("waiting for build units to exit"),
        "{reason}"
    );
    assert!(
        reason.contains(&format!("\"{unit}\" is active")),
        "{reason}"
    );
    assert!(reason.contains("MainPID 4242"), "{reason}");
    assert_eq!(diagnostic, "build-effect-still-present");
    // Nothing is removed while a build process may still write.
    assert!(staging.entry.exists());
}

#[test]
fn a_still_present_proxy_container_is_named_by_its_id() {
    let staging = staging();
    let container = format!("vonk-build-proxy-{}", staging.request.build_id.simple());
    let spark = Spark {
        unit_running: false,
        proxy_container: Some(container.clone()),
    };
    let (reason, diagnostic) = waiting(cleanup(&spark, &staging));
    // Quoted, the 49-character name survives the sanitizer's opaque-value
    // redaction: the operator sees which container the cleanup waits for.
    assert!(
        reason.contains(&format!("container \"{container}\" is still present")),
        "{reason}"
    );
    assert!(!reason.contains("redacted"), "{reason}");
    assert_eq!(diagnostic, "build-effect-still-present");
    assert!(staging.entry.exists());
}

#[test]
fn a_path_the_agent_may_not_remove_fails_with_its_path_and_errno() {
    if rustix::process::geteuid().is_root() {
        // Root bypasses the directory permission this fixture relies on.
        return;
    }
    let staging = staging();
    let parent = staging.entry.parent().unwrap().to_path_buf();
    std::fs::set_permissions(&parent, std::fs::Permissions::from_mode(0o555)).unwrap();
    let spark = Spark {
        unit_running: false,
        proxy_container: None,
    };
    let result = cleanup(&spark, &staging);
    std::fs::set_permissions(&parent, std::fs::Permissions::from_mode(0o755)).unwrap();
    // Waiting cannot end a permission denial: the order ends with its evidence
    // instead of being retried as an unconfirmed effect.
    let AgentResultResult::OutcomeFailed(failed) = result else {
        panic!("expected a failed cleanup, got {result:?}");
    };
    let errno = rustix::io::Errno::ACCESS.raw_os_error();
    assert!(
        failed
            .reason
            .contains(&format!("could not remove \"{}\"", staging.entry.display())),
        "{}",
        failed.reason
    );
    assert!(
        failed.reason.contains(&format!("os error {errno}")),
        "{}",
        failed.reason
    );
    let evidence = failed.evidence.unwrap();
    assert_eq!(
        evidence.diagnostic.as_deref(),
        Some(format!("build-path-os-error-{errno}").as_str())
    );
    assert_eq!(evidence.stage.as_deref(), Some("bounded-build-process"));
    assert!(failed.failure_kind.is_none());
}
