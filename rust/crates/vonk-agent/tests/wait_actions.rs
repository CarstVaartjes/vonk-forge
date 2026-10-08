//! Guard: the agent never reports a wait nobody can act on.
//!
//! An operation the agent cannot confirm ends as the typed `unknown` outcome
//! (`ExecutionResult::unknown`), the only way an `observing` result state is
//! produced. Three rules keep such a wait honest:
//!
//! * it carries evidence: `ExecutionResult::unknown` cannot be built without an
//!   `UnknownEvidence`, so the Controller always has the stage and cause to
//!   observe (checked by the compiler, and below on the wire message);
//! * every `WaitReason` the agent constructs has an advertised action in the
//!   table below: who moves it on. A reason without one fails this test, so a
//!   new wait cannot be added without deciding who resolves it;
//! * no source builds a waiting state by hand: only `outcome.rs` maps an
//!   outcome to a state word.
//!
//! Operator-only waits are listed explicitly; the list can only shrink.

use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};

use vonk_agent::outcome::{ExecutionResult, UnknownEvidence};
use vonk_agent_protocol::generated::{
    AgentOperation, AgentResultResult, AgentResultState, FailureStage, HelperErrorCode, WaitReason,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Action {
    /// The Controller observes the effect and re-issues the exact order (a
    /// restart-safe, idempotent operation), with no person involved.
    ControllerReissues,
    /// The upgraded agent reports its new identity; the Controller observes it.
    ControllerObservesIdentity,
    /// A job's process may still run: the stop route is the advertised action.
    OperatorStopRoute,
}

/// Every wait reason the agent constructs, and who resolves it.
const ADVERTISED: &[(WaitReason, Action)] = &[
    (
        WaitReason::AgentUpgradeAwaitingIdentity,
        Action::ControllerObservesIdentity,
    ),
    // Recovered at startup for any running operation; restart-safe operations
    // are re-issued, the Controller's lifecycle core decides for the rest.
    (
        WaitReason::AgentRestartInterrupted,
        Action::ControllerReissues,
    ),
    (WaitReason::StopUnconfirmed, Action::ControllerReissues),
    (WaitReason::CleanupUnconfirmed, Action::ControllerReissues),
    (
        WaitReason::StopMetadataUnconfirmed,
        Action::ControllerReissues,
    ),
    // Bounded local retries have already run; the step is idempotent.
    (
        WaitReason::ModelCustodyUnconfirmed,
        Action::ControllerReissues,
    ),
    (WaitReason::JobStopUnconfirmed, Action::OperatorStopRoute),
    (WaitReason::JobStateUncertain, Action::OperatorStopRoute),
];

/// Wait reasons whose only advertised action needs a person. A ceiling that
/// only falls: removing one lowers the number, and the test fails until it does.
const OPERATOR_ONLY_CEILING: usize = 2;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// The module's code without its test module and comments.
fn production_code(source: &str) -> String {
    // A file-level cfg applies to every item, including out-of-line fixtures.
    if source.lines().map(str::trim).find(|line| !line.is_empty()) == Some("#![cfg(test)]") {
        return String::new();
    }
    source
        .split("\n#[cfg(test)]\nmod ")
        .next()
        .unwrap_or(source)
        .lines()
        .filter(|line| !line.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n")
}

fn sources() -> Vec<(String, String)> {
    fn walk(directory: &Path, found: &mut Vec<PathBuf>) {
        for entry in fs::read_dir(directory).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                walk(&path, found);
            } else if path.extension().is_some_and(|extension| extension == "rs") {
                found.push(path);
            }
        }
    }
    let mut files = Vec::new();
    walk(&root().join("src"), &mut files);
    files.sort();
    files
        .into_iter()
        .map(|path| {
            let name = path
                .strip_prefix(root().join("src"))
                .unwrap()
                .to_string_lossy()
                .into_owned();
            (name, production_code(&fs::read_to_string(&path).unwrap()))
        })
        .collect()
}

/// The `WaitReason::Name` members a piece of code names.
fn wait_reasons_named(code: &str) -> BTreeSet<String> {
    let mut names = BTreeSet::new();
    let mut rest = code;
    while let Some(index) = rest.find("WaitReason::") {
        rest = &rest[index + "WaitReason::".len()..];
        let name: String = rest
            .chars()
            .take_while(|character| character.is_alphanumeric())
            .collect();
        if !name.is_empty() {
            names.insert(name);
        }
    }
    names
}

fn advertised_names() -> BTreeSet<String> {
    ADVERTISED
        .iter()
        .map(|(reason, _)| format!("{reason:?}"))
        .collect()
}

/// Reasons named in `sources` that the table does not advertise an action for.
fn unadvertised(sources: &[(String, String)]) -> Vec<String> {
    let advertised = advertised_names();
    sources
        .iter()
        .filter(|(name, _)| name != "outcome.rs")
        .flat_map(|(name, code)| {
            wait_reasons_named(code)
                .into_iter()
                .filter(|reason| !advertised.contains(reason))
                .map(move |reason| format!("{name}: WaitReason::{reason}"))
        })
        .collect()
}

/// Files that spell the waiting state themselves instead of getting it from an
/// outcome.
fn hand_built_waiting_states(sources: &[(String, String)]) -> Vec<String> {
    sources
        .iter()
        .filter(|(name, _)| name != "outcome.rs")
        .filter(|(_, code)| {
            code.contains("AgentResultState::Observing") || code.contains("WaitingForOperator")
        })
        .map(|(name, _)| name.clone())
        .collect()
}

#[test]
fn every_wait_the_agent_constructs_has_an_advertised_action() {
    let offenders = unadvertised(&sources());
    assert!(
        offenders.is_empty(),
        "name who resolves this wait in ADVERTISED, or do not wait: {offenders:?}"
    );
}

#[test]
fn the_table_lists_only_waits_the_agent_constructs() {
    // A row nothing constructs is a promise about a wait that no longer exists;
    // drop it so the table keeps describing what the agent does.
    let named: BTreeSet<String> = sources()
        .iter()
        .flat_map(|(_, code)| wait_reasons_named(code))
        .collect();
    let stale: Vec<_> = advertised_names().difference(&named).cloned().collect();
    assert!(stale.is_empty(), "no source constructs these: {stale:?}");
}

#[test]
fn a_person_is_the_advertised_action_of_at_most_the_ceiling() {
    let operator_only = ADVERTISED
        .iter()
        .filter(|(_, action)| *action == Action::OperatorStopRoute)
        .count();
    assert!(
        operator_only <= OPERATOR_ONLY_CEILING,
        "{operator_only} operator-only waits exceed the ceiling of {OPERATOR_ONLY_CEILING}"
    );
    assert_eq!(
        operator_only, OPERATOR_ONLY_CEILING,
        "lower OPERATOR_ONLY_CEILING to {operator_only}: the ceiling only falls"
    );
}

#[test]
fn only_an_outcome_produces_a_waiting_state() {
    let offenders = hand_built_waiting_states(&sources());
    assert!(
        offenders.is_empty(),
        "build a waiting state with ExecutionResult::unknown: {offenders:?}"
    );
}

#[test]
fn a_wait_reports_its_evidence_on_the_wire() {
    let finished = ExecutionResult::unknown(
        WaitReason::StopUnconfirmed,
        "workload stop remains unconfirmed",
        UnknownEvidence::at(FailureStage::Stop)
            .because("helper_io_failed")
            .helper(HelperErrorCode::OperationIo),
    )
    .finish_for(&AgentOperation::RecipeStop);

    assert_eq!(finished.state, AgentResultState::Observing);
    let AgentResultResult::OutcomeUnknown(unknown) = finished.result else {
        panic!("a wait is the unknown arm");
    };
    assert_eq!(unknown.wait_reason, WaitReason::StopUnconfirmed);
    let evidence = unknown.evidence.expect("a wait carries its evidence");
    assert_eq!(
        evidence.stage.as_deref(),
        Some(FailureStage::Stop.to_string().as_str())
    );
    assert_eq!(evidence.diagnostic.as_deref(), Some("helper_io_failed"));
    assert_eq!(evidence.helper_error_code.as_deref(), Some("operation_io"));
}

#[test]
fn the_guard_finds_what_it_forbids() {
    let seeded = vec![
        (
            "executor/mod.rs".to_owned(),
            production_code(
                r#"
fn wait() -> ExecutionResult {
    // WaitReason::LeaseLapsed in a comment is prose
    let state = AgentResultState::Observing;
    unconfirmed(WaitReason::ScopeChanged, "x", evidence)
}
#[cfg(test)]
mod tests {
    fn fixture() { let _ = WaitReason::StalePlan; }
}
"#,
            ),
        ),
        (
            "outcome.rs".to_owned(),
            "Self::Unknown(_) => AgentResultState::Observing".to_owned(),
        ),
    ];

    assert_eq!(
        unadvertised(&seeded),
        ["executor/mod.rs: WaitReason::ScopeChanged"]
    );
    assert_eq!(hand_built_waiting_states(&seeded), ["executor/mod.rs"]);
}
