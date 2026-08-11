use chrono::{DateTime, FixedOffset};
use serde_json::json;
use uuid::Uuid;
use vonk_agent_protocol::{AgentDirective, AgentProgress, canonical_json, parse_strict};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

fn deadline(value: &str) -> DateTime<FixedOffset> {
    DateTime::parse_from_rfc3339(value).unwrap()
}

fn progress() -> AgentProgress {
    AgentProgress {
        attempt: 2,
        deadline: deadline("2099-01-01T00:00:00+00:00"),
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
        node_id: NODE_ID.to_owned(),
        operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
        progress: json!({"phase": "executing"}),
        schema_version: 1,
    }
}

#[test]
fn progress_and_directive_round_trip_strictly() {
    let progress = progress();
    progress.validate().unwrap();
    let parsed: AgentProgress = parse_strict(&canonical_json(&progress).unwrap()).unwrap();
    assert_eq!(parsed, progress);

    let directive = AgentDirective {
        attempt: progress.attempt,
        cancel_requested: false,
        deadline: deadline("2099-01-01T00:00:30+00:00"),
        fence: progress.fence,
        job_id: progress.job_id,
        node_id: progress.node_id,
        operation_id: progress.operation_id,
        schema_version: progress.schema_version,
    };
    directive.validate().unwrap();
    let parsed: AgentDirective = parse_strict(&canonical_json(&directive).unwrap()).unwrap();
    assert_eq!(parsed, directive);
}

#[test]
fn heartbeat_messages_reject_invalid_attempts_and_nodes() {
    let mut invalid_attempt = progress();
    invalid_attempt.attempt = 0;
    assert!(invalid_attempt.validate().is_err());

    let mut invalid_node = progress();
    invalid_node.node_id = "spark-one".to_owned();
    assert!(invalid_node.validate().is_err());
}
