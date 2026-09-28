use chrono::{DateTime, FixedOffset};
use serde_json::json;
use uuid::Uuid;
use vonk_agent_protocol::{AgentDirective, AgentProgress, canonical_json, parse_strict};

fn deadline(value: &str) -> DateTime<FixedOffset> {
    DateTime::parse_from_rfc3339(value).unwrap()
}

fn progress() -> AgentProgress {
    AgentProgress {
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        progress: serde_json::from_value(json!({"phase": "executing"})).unwrap(),
    }
}

#[test]
fn progress_and_directive_round_trip_strictly() {
    let progress = progress();
    progress.validate().unwrap();
    let parsed: AgentProgress = parse_strict(&canonical_json(&progress).unwrap()).unwrap();
    assert_eq!(parsed, progress);

    let directive = AgentDirective {
        cancel_requested: false,
        deadline: deadline("2099-01-01T00:00:30+00:00"),
        fence: progress.fence,
    };
    let parsed: AgentDirective = parse_strict(&canonical_json(&directive).unwrap()).unwrap();
    assert_eq!(parsed, directive);
}

#[test]
fn nested_progress_rejects_invalid_totals_scalars_and_unknown_fields() {
    for document in [
        json!({"phase":"copying", "completed_bytes":true}),
        json!({"phase":"copying", "completed_bytes":10, "total_bytes":5, "total_bytes_known":true}),
        json!({"phase":"copying", "total_bytes":5}),
        json!({"phase":"copying", "rate":1.0}),
        json!({"phase":"copying", "observed_at":"2026-09-08T00:00:00"}),
        json!({"phase":"copying", "members":[{"member_id":"a","phase":"copying"},{"member_id":"a","phase":"copying"}]}),
    ] {
        let mut message = serde_json::to_value(progress()).unwrap();
        message["progress"] = document;
        assert!(
            serde_json::from_value::<AgentProgress>(message)
                .map_or(true, |message| message.validate().is_err())
        );
    }
}
