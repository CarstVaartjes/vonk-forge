#![cfg(test)]

use super::*;

const PROVIDED_LINE: &str =
    "host:1:1 [0] NCCL INFO NCCL_IB_HCA set by environment to =rocep1s0f1:1,roceP2p1s0f1:1\n";
const BOTH: &str = "host:1:1 [0] NCCL INFO NET/IB : Using [0]rocep1s0f1:1/RoCE [1]roceP2p1s0f1:1/RoCE ; OOB enP7s7:10.0.0.2<0>\n";
const ONE: &str =
    "host:1:1 [0] NCCL INFO NET/IB : Using [0]rocep1s0f1:1/RoCE ; OOB enP7s7:10.0.0.2<0>\n";
const MERGED: &str = "NET/IB : Using [0]rocep1s0f1+roceP2p1s0f1:1/RoCE ; OOB x\n";

#[test]
fn one_of_two_launched_rails_is_observed_as_such() {
    let tail = format!("loading\n{PROVIDED_LINE}{ONE}");
    assert_eq!(
        Rails::Unseen.after(&tail),
        Rails::Observed {
            provided: 2,
            used: 1
        }
    );
}

#[test]
fn the_lines_may_arrive_in_different_reads() {
    let first = Rails::Unseen.after(PROVIDED_LINE);
    assert_eq!(first, Rails::Provided(2));
    // The launched line scrolled out of the tail before the other appeared.
    assert_eq!(
        first.after(BOTH),
        Rails::Observed {
            provided: 2,
            used: 2
        }
    );
    // Later reads that show neither line leave what was observed.
    assert_eq!(first.after(BOTH).after("serving\n"), first.after(BOTH));
}

#[test]
fn a_device_merged_into_one_item_counts_each_name() {
    assert_eq!(used_devices(MERGED), Some(2));
}

#[test]
fn without_both_lines_nothing_is_guessed() {
    assert_eq!(Rails::Unseen.after("loading weights\n"), Rails::Unseen);
    // NET/IB without the launched value (NCCL_DEBUG not showing params).
    assert_eq!(Rails::Unseen.after(BOTH), Rails::Unseen);
    // An exclusion list names no device count.
    let excluded = "NCCL_IB_HCA set by environment to ^mlx5_0\n";
    assert_eq!(provided_devices(excluded), None);
}

#[test]
fn only_a_completed_observation_is_reported_and_a_stop_forgets_it() {
    let directory = tempfile::tempdir().unwrap();
    let store = Store::new(directory.path());
    let run = Uuid::new_v4();
    store.note(run, PROVIDED_LINE);
    assert!(store.snapshot().is_empty());
    store.note(run, &format!("{PROVIDED_LINE}{ONE}"));
    let seen = store.snapshot();
    assert_eq!((seen[0].provided, seen[0].used), (2, 1));
    store.forget(run);
    assert!(store.snapshot().is_empty());
}

const VECTOR: &str = include_str!(
    "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/fabric-rails-v1.json"
);

#[test]
fn the_shared_logs_read_as_the_shared_observation() {
    let vector: serde_json::Value = serde_json::from_str(VECTOR).unwrap();
    for log in vector["logs"].as_array().unwrap() {
        let observed = match Rails::Unseen.after(log["tail"].as_str().unwrap()) {
            Rails::Observed { provided, used } => {
                Some(serde_json::json!({"provided": provided, "used": used}))
            }
            _ => None,
        };
        let expected = (!log["observed"].is_null()).then(|| log["observed"].clone());
        assert_eq!(observed, expected, "{}", log["name"]);
    }
}

#[test]
fn the_shared_cases_are_the_contract_the_agent_reports() {
    let vector: serde_json::Value = serde_json::from_str(VECTOR).unwrap();
    for case in vector["cases"].as_array().unwrap() {
        let rails: Vec<FabricRails> = serde_json::from_value(case["fabric_rails"].clone())
            .unwrap_or_else(|error| panic!("{}: {error}", case["name"]));
        assert_eq!(serde_json::to_value(&rails).unwrap(), case["fabric_rails"]);
    }
}
