use super::*;
use std::sync::atomic::{AtomicUsize, Ordering};

const INTERVAL: Duration = Duration::from_secs(5);
const GIB: u64 = 1024 * 1024 * 1024;

fn report(problem: Option<PageCacheDropProblem>) -> PageCacheDropReport {
    PageCacheDropReport {
        request_id: Uuid::new_v4(),
        dropped_at: Utc::now().fixed_offset(),
        cached_before_bytes: problem.is_none().then_some(9 * GIB),
        cached_after_bytes: problem.is_none().then_some(GIB),
        mem_available_after_bytes: None,
        sync: None,
        problem,
    }
}

/// A dropper that counts its drops and answers with `problem`.
fn counting(problem: Option<PageCacheDropProblem>) -> (Arc<LoadPhases>, Arc<AtomicUsize>) {
    let drops = Arc::new(AtomicUsize::new(0));
    let counted = Arc::clone(&drops);
    let dropper: Dropper = Arc::new(move || {
        counted.fetch_add(1, Ordering::SeqCst);
        Box::pin(async move { report(problem) })
    });
    (LoadPhases::new(dropper, INTERVAL), drops)
}

fn ticking(phases: &LoadPhases) -> bool {
    phases.lock().ticking
}

#[tokio::test(start_paused = true)]
async fn a_loading_run_is_dropped_every_interval_and_never_after_it_is_ready() {
    let (phases, drops) = counting(None);
    let load = phases.begin(Uuid::new_v4());
    load.drop_now().await;
    tokio::time::sleep(INTERVAL * 3 + INTERVAL / 2).await;
    assert_eq!(drops.load(Ordering::SeqCst), 4);
    let evidence = load.finish().expect("drops were made");
    assert_eq!(evidence.drops, 4);
    assert_eq!(evidence.problems, 0);
    assert_eq!(evidence.reclaimed_bytes, 4 * 8 * GIB);
    assert!(evidence.first.is_some() && evidence.last.is_some());
    tokio::time::sleep(INTERVAL * 10).await;
    assert_eq!(drops.load(Ordering::SeqCst), 4);
    assert!(!ticking(&phases));
}

#[tokio::test(start_paused = true)]
async fn a_launched_rank_keeps_its_drops_until_its_deadline_and_the_cadence_then_ends() {
    // A worker rank has no readiness wait on its node: its start deadline
    // bounds its drops.
    let (phases, drops) = counting(None);
    let load = phases.begin(Uuid::new_v4());
    load.drop_now().await;
    let snapshot = load
        .release_until(Instant::now() + INTERVAL * 2 + INTERVAL / 2)
        .expect("the pre-start drop");
    assert_eq!(snapshot.drops, 1);
    tokio::time::sleep(INTERVAL * 20).await;
    assert_eq!(drops.load(Ordering::SeqCst), 3);
    assert!(!ticking(&phases));
}

#[tokio::test(start_paused = true)]
async fn the_masters_readiness_phase_adopts_its_launched_ranks_drops_and_ends_them_at_ready() {
    let (phases, drops) = counting(None);
    let run = Uuid::new_v4();
    let launched = phases.begin(run);
    launched.drop_now().await;
    let first = launched
        .release_until(Instant::now() + INTERVAL * 100)
        .and_then(|evidence| evidence.first)
        .unwrap();
    tokio::time::sleep(INTERVAL + INTERVAL / 2).await;
    let readiness = phases.begin(run);
    tokio::time::sleep(INTERVAL).await;
    let evidence = readiness.finish().unwrap();
    assert_eq!(evidence.first, Some(first));
    assert_eq!(evidence.drops, 3);
    tokio::time::sleep(INTERVAL * 10).await;
    assert_eq!(drops.load(Ordering::SeqCst), 3);
}

#[tokio::test(start_paused = true)]
async fn an_abandoned_start_ends_its_drops() {
    let (phases, drops) = counting(None);
    drop(phases.begin(Uuid::new_v4()));
    tokio::time::sleep(INTERVAL * 10).await;
    assert_eq!(drops.load(Ordering::SeqCst), 0);
    assert!(!ticking(&phases));
}

#[tokio::test(start_paused = true)]
async fn a_drop_the_helper_cannot_make_is_noted_and_the_cadence_continues() {
    let (phases, drops) = counting(Some(PageCacheDropProblem::NotPermitted));
    let load = phases.begin(Uuid::new_v4());
    load.drop_now().await;
    tokio::time::sleep(INTERVAL * 2 + INTERVAL / 2).await;
    let evidence = load.finish().unwrap();
    assert_eq!(drops.load(Ordering::SeqCst), 3);
    assert_eq!((evidence.drops, evidence.problems), (0, 3));
    assert_eq!(evidence.reclaimed_bytes, 0);
    assert_eq!(
        evidence.last_problem.and_then(|report| report.problem),
        Some(PageCacheDropProblem::NotPermitted)
    );
}

#[tokio::test(start_paused = true)]
async fn concurrent_loads_share_one_cadence() {
    let (phases, drops) = counting(None);
    let first = phases.begin(Uuid::new_v4());
    let second = phases.begin(Uuid::new_v4());
    tokio::time::sleep(INTERVAL * 2 + INTERVAL / 2).await;
    assert_eq!(drops.load(Ordering::SeqCst), 2);
    assert_eq!(first.finish().unwrap().drops, 2);
    assert_eq!(second.finish().unwrap().drops, 2);
}

#[test]
fn an_ask_is_answered_only_by_the_helpers_report_for_it() {
    use std::io::{Read, Write};
    let root = tempfile::tempdir().unwrap();
    let socket = root.path().join("helper.sock");
    assert_eq!(
        ask(&socket).problem,
        Some(PageCacheDropProblem::HelperUnavailable)
    );
    let listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    let helper = std::thread::spawn(move || {
        for answer_for_request in [true, false] {
            let (mut stream, _) = listener.accept().unwrap();
            let mut prefix = [0; 4];
            stream.read_exact(&mut prefix).unwrap();
            let mut body = vec![0; u32::from_be_bytes(prefix) as usize];
            stream.read_exact(&mut body).unwrap();
            let request: PageCacheDropRequest = serde_json::from_slice(&body).unwrap();
            let mut answer = report(None);
            if answer_for_request {
                answer.request_id = request.request_id;
            }
            let body = canonical_generated_json(&answer).unwrap();
            stream
                .write_all(&(body.len() as u32).to_be_bytes())
                .unwrap();
            stream.write_all(&body).unwrap();
        }
    });
    let answered = ask(&socket);
    assert_eq!(answered.problem, None);
    assert_eq!(answered.cached_after_bytes, Some(GIB));
    assert_eq!(
        ask(&socket).problem,
        Some(PageCacheDropProblem::HelperUnavailable)
    );
    helper.join().unwrap();
}

#[tokio::test(start_paused = true)]
async fn a_stop_ends_a_launched_ranks_drops_before_its_deadline() {
    let (phases, drops) = counting(None);
    let run = Uuid::new_v4();
    phases
        .begin(run)
        .release_until(Instant::now() + INTERVAL * 100);
    tokio::time::sleep(INTERVAL + INTERVAL / 2).await;
    phases.end(run);
    tokio::time::sleep(INTERVAL * 10).await;
    assert_eq!(drops.load(Ordering::SeqCst), 1);
    assert!(!ticking(&phases));
}

#[tokio::test(start_paused = true)]
async fn a_load_after_the_cadence_ended_starts_it_again() {
    let (phases, drops) = counting(None);
    phases.begin(Uuid::new_v4()).finish();
    tokio::time::sleep(INTERVAL * 3).await;
    assert!(!ticking(&phases));
    let load = phases.begin(Uuid::new_v4());
    tokio::time::sleep(INTERVAL * 2 + INTERVAL / 2).await;
    assert_eq!(load.finish().unwrap().drops, 2);
    assert_eq!(drops.load(Ordering::SeqCst), 2);
}
