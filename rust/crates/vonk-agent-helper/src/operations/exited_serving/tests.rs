#![cfg(test)]
//! What the helper does with serving containers whose process has exited.
use super::*;
use crate::operations::test_support::*;

const ENDED: &str = "c0000000-0000-4000-8000-00000000000c";
const LIVE: &str = "d0000000-0000-4000-8000-00000000000d";
const JOB_RUN: &str = "a0000000-0000-4000-8000-00000000000a";
const JOB: &str = "a1000000-0000-4000-8000-0000000000a1";

fn exited_at() -> chrono::DateTime<chrono::Utc> {
    chrono::DateTime::parse_from_rfc3339(EXITED_AT)
        .unwrap()
        .with_timezone(&chrono::Utc)
}

fn after(hours: i64) -> chrono::DateTime<chrono::Utc> {
    exited_at() + chrono::TimeDelta::hours(hours)
}

/// The agent's claim directory, readable and empty.
fn claims(executor: &OperationExecutor<Host>) -> PathBuf {
    let root = executor.roots.agent_data.join("run-metadata");
    fs::create_dir_all(&root).unwrap();
    root
}

/// The lifecycle claim the agent keeps while the Controller wants the run.
fn claim(executor: &OperationExecutor<Host>, run: &str) {
    let metadata = claims(executor).join(run);
    fs::create_dir_all(&metadata).unwrap();
    fs::write(metadata.join("lifecycle.json"), b"{}").unwrap();
}

fn id(target: &str) -> String {
    job(target, target, "exited").id()
}

fn removed(host: &Host, target: &str) -> Option<usize> {
    let id = id(target);
    host.calls
        .lock()
        .unwrap()
        .iter()
        .position(|call| call == &["rm".to_owned(), id.clone()])
}

#[test]
fn an_ended_runs_exited_container_goes_after_its_grace_with_its_output_journaled() {
    let host = Host::with(&[job(ENDED, ENDED, "exited")]);
    let (_temp, executor) = host_executor(&host);
    // The Controller named the run ended; the agent retired its claim and
    // left the run's metadata as history.
    claims(&executor);
    fs::create_dir_all(executor.roots.agent_data.join("run-metadata").join(ENDED)).unwrap();

    executor.sweep_exited_serving_at(after(23));
    assert!(host.present(ENDED), "removed within its grace");

    executor.sweep_exited_serving_at(after(25));
    assert!(
        !host.present(ENDED),
        "an ended run's exited container was kept"
    );
    // Its output is read, by its exact id, before the container is removed.
    let logs = host
        .calls
        .lock()
        .unwrap()
        .iter()
        .position(|call| {
            call.first().map(String::as_str) == Some("logs") && call.last() == Some(&id(ENDED))
        })
        .expect("its output was never read");
    assert!(logs < removed(&host, ENDED).unwrap());
}

#[test]
fn a_run_the_agent_still_claims_keeps_its_container() {
    // A live run (planned, starting, running, stopping, lost) keeps its claim
    // however long its process has been gone: its own Stop removes it.
    let host = Host::with(&[job(LIVE, LIVE, "exited"), job(ENDED, ENDED, "running")]);
    let (_temp, executor) = host_executor(&host);
    claim(&executor, LIVE);

    executor.sweep_exited_serving_at(after(24 * 14));

    assert!(host.present(LIVE));
    assert!(host.present(ENDED), "a running container was removed");
    assert_eq!(removed(&host, LIVE), None);
    assert_eq!(removed(&host, ENDED), None);
}

#[test]
fn unreadable_claims_or_listing_remove_nothing() {
    let host = Host::with(&[job(ENDED, ENDED, "exited")]);
    let (_temp, executor) = host_executor(&host);
    // No claim directory: unknown is not none.
    executor.sweep_exited_serving_at(after(24 * 14));
    assert!(host.present(ENDED));

    let host = Host {
        listing_fails: true,
        ..Host::with(&[job(ENDED, ENDED, "exited")])
    };
    let (_temp, executor) = host_executor(&host);
    claims(&executor);
    executor.sweep_exited_serving_at(after(24 * 14));
    assert!(host.present(ENDED));
}

#[test]
fn a_job_container_is_never_selected() {
    let host = Host::with(&[job(JOB_RUN, JOB, "exited")]);
    let (_temp, executor) = host_executor(&host);
    claims(&executor);

    executor.sweep_exited_serving_at(after(24 * 14));

    assert!(host.present(JOB));
    assert_eq!(removed(&host, JOB), None);
}

/// The class guard: every kind of container the helper creates has a
/// collector. A one-shot preflight removes itself (`--rm`); a job container
/// with no owner goes by the stray-job sweep; a serving container of an ended
/// run goes by this sweep. Only the live workload remains.
#[test]
fn every_container_kind_the_helper_creates_is_collected_once_unowned() {
    assert!(
        crate::runtime_preflight::run_arguments("vonk-preflight", "image")
            .iter()
            .any(|argument| argument == "--rm")
    );
    let host = Host::with(&[
        job(JOB_RUN, JOB, "exited"),
        job(ENDED, ENDED, "exited"),
        job(LIVE, LIVE, "running"),
    ]);
    let (_temp, executor) = host_executor(&host);
    claim(&executor, LIVE);

    executor.sweep_stray_jobs();
    executor.sweep_exited_serving_at(after(25));

    let left: Vec<_> = host
        .containers
        .lock()
        .unwrap()
        .iter()
        .map(|container| container.target.clone())
        .collect();
    assert_eq!(left, [LIVE]);
}

#[test]
fn protected_serving_front_page_does_not_starve_ended_tail() {
    let mut containers = vec![job(ENDED, ENDED, "exited")];
    containers.extend((0..MAX_LISTED).map(|n| {
        let run = format!("f{n:07x}-0000-4000-8000-{n:012x}");
        job(&run, &run, "exited")
    }));
    let host = Host::with(&containers);
    let (_temp, executor) = host_executor(&host);
    for container in containers.iter().skip(1) {
        claim(&executor, &container.run);
    }
    executor.sweep_exited_serving_at(after(25));
    assert!(!host.present(ENDED));
    assert_eq!(host.containers.lock().unwrap().len(), MAX_LISTED);
}
