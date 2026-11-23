#![cfg(test)]
use super::*;

const MIB: u64 = 1024 * 1024;

/// Run one measuring window at `rate` bytes per second with every stream busy.
fn window(state: &mut State, now: &mut Instant, rate: u64) -> bool {
    state.active = state.limit;
    state.peak_active = state.limit;
    state.window_bytes += rate * WINDOW.as_secs();
    *now += WINDOW;
    state.evaluate(*now)
}

fn state() -> (State, Instant) {
    let now = Instant::now();
    (State::new(INITIAL_STREAMS, MAX_STREAMS, now), now)
}

#[test]
fn climbs_while_throughput_grows_to_the_cap() {
    let (mut state, mut now) = state();
    assert_eq!(state.limit, 4);
    window(&mut state, &mut now, 100 * MIB);
    assert_eq!(state.limit, 6);
    window(&mut state, &mut now, 150 * MIB);
    assert_eq!(state.limit, 8);
    window(&mut state, &mut now, 200 * MIB);
    assert_eq!(state.limit, 8, "never above the cap");
}

#[test]
fn returns_to_the_previous_limit_and_holds_when_a_step_did_not_pay() {
    let (mut state, mut now) = state();
    window(&mut state, &mut now, 100 * MIB);
    assert_eq!(state.limit, 6);
    // Only 5% more: not worth six streams.
    window(&mut state, &mut now, 105 * MIB);
    assert_eq!(state.limit, 4);
    // The ramp stays paused for the hold, however saturated the streams are.
    for _ in 0..HOLD_WINDOWS {
        window(&mut state, &mut now, 100 * MIB);
        assert_eq!(state.limit, 4);
    }
    window(&mut state, &mut now, 100 * MIB);
    assert_eq!(state.limit, 6, "the ramp resumes after the hold");
}

#[test]
fn does_not_climb_while_streams_are_idle() {
    let (mut state, mut now) = state();
    state.active = 1;
    state.peak_active = 1;
    state.window_bytes = 100 * MIB * WINDOW.as_secs();
    now += WINDOW;
    assert!(!state.evaluate(now));
    assert_eq!(state.limit, 4);
}

#[test]
fn a_window_is_evaluated_only_when_it_has_elapsed() {
    let (mut state, now) = state();
    state.active = 4;
    state.peak_active = 4;
    state.window_bytes = 100 * MIB;
    assert!(!state.evaluate(now + WINDOW / 2));
    assert_eq!(state.limit, 4);
}

#[test]
fn transient_failures_halve_the_limit_down_to_the_floor() {
    let (mut state, mut now) = state();
    window(&mut state, &mut now, 100 * MIB);
    window(&mut state, &mut now, 150 * MIB);
    assert_eq!(state.limit, 8);
    assert!(state.throttled(now));
    assert_eq!(state.limit, 4);
    assert!(state.throttled(now));
    assert_eq!(state.limit, 2);
    assert!(!state.throttled(now));
    assert_eq!(state.limit, 2);
}

#[tokio::test(start_paused = true)]
async fn waits_for_a_slot_and_wakes_when_one_is_released() {
    let governor = StreamGovernor::new(2, 2);
    let first = governor.acquire().await;
    let _second = governor.acquire().await;
    let waiting = tokio::time::timeout(Duration::from_millis(100), governor.acquire()).await;
    assert!(waiting.is_err(), "a third stream waits at a limit of two");
    drop(first);
    let third = tokio::time::timeout(Duration::from_secs(5), governor.acquire())
        .await
        .expect("a released slot wakes a waiting stream");
    drop(third);
    assert_eq!(governor.limit(), 2);
}
