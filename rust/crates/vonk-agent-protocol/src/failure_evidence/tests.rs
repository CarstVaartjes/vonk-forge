#![cfg(test)]
use super::{ExitCause, FailureDiagnostics, LOG_TAIL_BYTES, is_omission_marker, omission_marker};

#[test]
fn the_shared_omission_vector_reads_the_marker_as_it_is_written() {
    // The Controller's copy of the marker is held to this vector too.
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/fixtures/log-tail-omission-v1.json"
    ))
    .expect("log-tail omission vector");
    let lines = |kind: &str| {
        vector["markers"][kind]
            .as_array()
            .expect("marker lines")
            .iter()
            .map(|line| line.as_str().expect("marker line").to_owned())
            .collect::<Vec<_>>()
    };
    for line in lines("valid") {
        assert!(is_omission_marker(&line), "{line}");
    }
    for line in lines("invalid") {
        assert!(!is_omission_marker(&line), "{line}");
    }
    assert!(lines("valid").contains(&omission_marker(7943)));
}

#[test]
fn the_shared_exit_cause_vector_names_every_cause_and_token() {
    // The Controller reads its host-memory-guard token from this vector.
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/exit-causes-v1.json"
    ))
    .expect("exit-causes vector");
    let tokens = vector["tokens"].as_object().expect("tokens");
    assert_eq!(tokens.len(), ExitCause::ALL.len());
    for cause in ExitCause::ALL {
        assert_eq!(
            tokens.get(&format!("{cause:?}")).and_then(|v| v.as_str()),
            Some(cause.token()),
            "{cause:?}"
        );
    }
}

#[test]
fn only_an_ending_that_skipped_cleanup_excuses_what_a_process_left() {
    // The helper's summaries for each exit: an OOMKilled=true exit is the OOM
    // killer's SIGKILL, 139 a native SIGSEGV, 143 a SIGTERM the process
    // could clean up after, 1 its own exception.
    for (summary, cause, skipped) in [
        (
            "exit_code=137 oom_killed=true exit_cause=oom_killed",
            ExitCause::OomKilled,
            true,
        ),
        (
            "reason=workload.host_memory_exhausted exit_cause=host_memory_exhausted",
            ExitCause::HostMemoryExhausted,
            true,
        ),
        (
            "exit_code=139 exit_cause=crashed_by_signal",
            ExitCause::CrashedBySignal,
            true,
        ),
        (
            "exit_code=143 exit_cause=unclassified",
            ExitCause::Unclassified,
            false,
        ),
        (
            "exit_code=1 exit_cause=python_exception exception=\"exit_cause=oom_killed\"",
            ExitCause::PythonException,
            false,
        ),
    ] {
        assert_eq!(ExitCause::of_summary(summary), Some(cause), "{summary}");
        assert_eq!(cause.skipped_cleanup(), skipped, "{summary}");
        assert_eq!(ExitCause::from_token(cause.token()), Some(cause));
    }
}

#[test]
fn failure_diagnostics_enforce_utf8_byte_bound() {
    let mut value: FailureDiagnostics = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/failure-diagnostics-v1.json"
    ))
    .unwrap();
    value.stdout.text = "😀".repeat(LOG_TAIL_BYTES);
    value.stderr.text = "😀".repeat(LOG_TAIL_BYTES);
    assert!(value.validate().is_err());
}

#[test]
fn canonical_python_failure_diagnostics_vector_round_trips() {
    let document: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/failure-diagnostics-v1.json"
    ))
    .unwrap();
    let diagnostics: FailureDiagnostics = serde_json::from_value(document.clone()).unwrap();
    diagnostics.validate().unwrap();
    assert_eq!(serde_json::to_value(diagnostics).unwrap(), document);
}
