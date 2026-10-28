//! Fault injection over simulated host time series, one per rule, with the
//! cases the old policy got wrong: a load that swaps steadily is never killed.
use super::*;
use vonk_agent_protocol::host_memory_guard_policy::SAMPLE_INTERVAL;

const GIB: u64 = 1024 * 1024 * 1024;
const PAGE: u64 = 4096;

/// One Spark under load, advanced one sample at a time.
#[derive(Clone, Copy)]
struct Host {
    available: u64,
    free: u64,
    swap_total: u64,
    swap_free: u64,
    avg10: f64,
    avg60: f64,
    swap_in_per_s: u64,
    swap_out_per_s: u64,
    refault_per_s: u64,
    counters: Counters,
}

impl Host {
    fn serving() -> Self {
        Self {
            available: 20 * GIB,
            free: 10 * GIB,
            swap_total: 64 * GIB,
            swap_free: 64 * GIB,
            avg10: 0.0,
            avg60: 0.0,
            swap_in_per_s: 0,
            swap_out_per_s: 0,
            refault_per_s: 0,
            counters: Counters::default(),
        }
    }
    fn tick(&mut self) -> Sample {
        let seconds = SAMPLE_INTERVAL.as_secs_f64();
        let step = |rate: u64| (rate as f64 * seconds) as u64;
        self.counters.swap_in += step(self.swap_in_per_s);
        self.counters.swap_out += step(self.swap_out_per_s);
        self.counters.refault += step(self.refault_per_s);
        // Swap fills at the swap-out rate and drains at the swap-in rate.
        self.swap_free = self
            .swap_free
            .saturating_sub(step(self.swap_out_per_s) * PAGE)
            .saturating_add(step(self.swap_in_per_s) * PAGE)
            .min(self.swap_total);
        Sample {
            available_bytes: Some(self.available),
            free_bytes: Some(self.free),
            swap_total_bytes: Some(self.swap_total),
            swap_free_bytes: Some(self.swap_free),
            full_avg10: Some(self.avg10),
            full_avg60: Some(self.avg60),
            counters: Some(self.counters),
        }
    }
}

/// The first tick at which the decision kills, within `ticks`.
fn run(
    host: &mut Host,
    decision: &mut Decision,
    start: Instant,
    ticks: std::ops::Range<u32>,
    loading: bool,
) -> Option<(u32, Trigger)> {
    for tick in ticks {
        let sample = host.tick();
        if let Some(trigger) =
            decision.observe(start + SAMPLE_INTERVAL * tick, sample, false, true, loading)
        {
            return Some((tick, trigger));
        }
    }
    None
}

fn run_with_line(
    host: &mut Host,
    decision: &mut Decision,
    start: Instant,
    ticks: std::ops::Range<u32>,
) -> Option<(u32, Trigger)> {
    for tick in ticks {
        let sample = host.tick();
        if let Some(trigger) =
            decision.observe(start + SAMPLE_INTERVAL * tick, sample, true, true, true)
        {
            return Some((tick, trigger));
        }
    }
    None
}

fn ticks(duration: Duration) -> u32 {
    (duration.as_millis() / SAMPLE_INTERVAL.as_millis()) as u32
}

/// A big dual load streaming its staging copies to swap: MemAvailable pinned
/// near the watermark, full PSI from kswapd reclaim, swap filling steadily.
fn steady_swapping_load() -> Host {
    Host {
        available: 700 * 1024 * 1024,
        free: 1536 * 1024 * 1024,
        avg10: 45.0,
        avg60: 50.0,
        swap_out_per_s: 20_000,
        swap_in_per_s: 300,
        refault_per_s: 2_000,
        ..Host::serving()
    }
}

#[test]
fn a_load_that_swaps_steadily_for_ten_minutes_is_never_killed() {
    // Break caught: the 30%-for-3 s rule killed exactly this (GLM 5.3 Flash,
    // MiMo V2.5 and DSV4 duals in the sweep) while 48 GiB of swap stayed free.
    for loading in [true, false] {
        let mut host = steady_swapping_load();
        let mut decision = Decision::default();
        let ten_minutes = ticks(Duration::from_secs(10 * 60));
        // 20k pages/s for 10 minutes moves ~45 GiB: the 64 GiB swap absorbs it.
        assert_eq!(
            run(
                &mut host,
                &mut decision,
                Instant::now(),
                0..ten_minutes,
                loading
            ),
            None,
            "loading={loading}"
        );
        assert!(host.swap_free > 10 * GIB);
    }
}

#[test]
fn a_real_runaway_is_killed_within_the_memory_and_swap_bound() {
    let mut host = steady_swapping_load();
    let mut decision = Decision::default();
    let start = Instant::now();
    // The same load keeps allocating until swap is gone too.
    host.swap_out_per_s = 200_000;
    let limit = ticks(Duration::from_secs(120));
    let (tick, trigger) = run(&mut host, &mut decision, start, 0..limit, true).unwrap();
    assert!(matches!(
        trigger,
        Trigger::MemoryAndSwapExhausted | Trigger::SwapFilling
    ));
    // Swap filled within ~84 s at 200k pages/s; the kill follows within its
    // sustain bound, not minutes later.
    let filled = ((host.swap_total - host.swap_free) / PAGE / 200_000) as u32;
    assert!(
        tick <= ticks(Duration::from_secs(90)) + ticks(SWAP_FILLING_SUSTAIN),
        "{tick} {filled}"
    );
    assert!(
        decision.pressure_duration(start + SAMPLE_INTERVAL * tick, trigger)
            >= SWAP_EXHAUSTED_SUSTAIN
    );
}

#[test]
fn r1_memory_and_swap_exhausted_kills_after_two_seconds_and_not_before() {
    let mut host = Host {
        available: 512 * 1024 * 1024,
        swap_free: GIB,
        ..Host::serving()
    };
    let mut decision = Decision::default();
    let start = Instant::now();
    let bound = ticks(SWAP_EXHAUSTED_SUSTAIN);
    assert_eq!(run(&mut host, &mut decision, start, 0..bound, false), None);
    assert_eq!(
        run(&mut host, &mut decision, start, bound..bound + 1, false),
        Some((bound, Trigger::MemoryAndSwapExhausted))
    );
    // Low memory with swap still free is the cushion working: no kill.
    let mut cushioned = Host {
        available: 512 * 1024 * 1024,
        swap_free: 30 * GIB,
        ..Host::serving()
    };
    let mut decision = Decision::default();
    assert_eq!(
        run(&mut cushioned, &mut decision, start, 0..400, false),
        None
    );
}

#[test]
fn r1_without_swap_keeps_the_256_mib_rule() {
    let mut host = Host {
        available: EXHAUSTED_BYTES - 1,
        swap_total: 0,
        swap_free: 0,
        ..Host::serving()
    };
    let mut decision = Decision::default();
    let start = Instant::now();
    let bound = ticks(SUSTAINED_PRESSURE);
    assert_eq!(run(&mut host, &mut decision, start, 0..bound, false), None);
    assert_eq!(
        run(&mut host, &mut decision, start, bound..bound + 1, false),
        Some((bound, Trigger::MemoryExhausted))
    );
    // A tight kit at 3 GiB never trips it.
    let mut tight = Host {
        available: 3 * GIB,
        swap_total: 0,
        swap_free: 0,
        ..Host::serving()
    };
    assert_eq!(
        run(&mut tight, &mut Decision::default(), start, 0..400, false),
        None
    );
}

#[test]
fn r2_swap_nearly_full_kills_only_while_still_filling() {
    let filling = Host {
        available: 3 * GIB,
        swap_free: 5 * GIB,
        swap_out_per_s: 5_000,
        ..Host::serving()
    };
    let start = Instant::now();
    let mut host = filling;
    let mut decision = Decision::default();
    let (tick, trigger) = run(&mut host, &mut decision, start, 0..100, false).unwrap();
    assert_eq!(trigger, Trigger::SwapFilling);
    // The first sample has no rate yet; then three seconds of filling.
    assert_eq!(tick, ticks(SWAP_FILLING_SUSTAIN) + 1);
    // The same full swap that no longer grows is settled, not a runaway.
    let mut settled = Host {
        swap_out_per_s: 0,
        ..filling
    };
    assert_eq!(
        run(&mut settled, &mut Decision::default(), start, 0..400, false),
        None
    );
}

#[test]
fn r3_a_refault_storm_without_headroom_kills_after_ten_seconds() {
    let storm = Host {
        available: 2 * GIB,
        swap_free: 3 * GIB,
        avg10: 70.0,
        avg60: 70.0,
        swap_in_per_s: 30_000,
        refault_per_s: 30_000,
        ..Host::serving()
    };
    let start = Instant::now();
    let mut host = storm;
    let (tick, trigger) = run(&mut host, &mut Decision::default(), start, 0..200, true).unwrap();
    assert_eq!(trigger, Trigger::Thrashing);
    assert!(tick >= ticks(THRASH_SUSTAIN) && tick <= ticks(THRASH_SUSTAIN) + 2);
    // The same storm with 30 GiB of swap left is a load paging, not a wedge.
    let mut headroom = Host {
        swap_free: 30 * GIB,
        ..storm
    };
    assert_eq!(
        run(&mut headroom, &mut Decision::default(), start, 0..400, true),
        None
    );
}

#[test]
fn r4_serving_pressure_kills_after_thirty_seconds_and_loading_waits_longer() {
    let pressured = Host {
        avg10: 65.0,
        avg60: 70.0,
        ..Host::serving()
    };
    let start = Instant::now();
    let bound = ticks(SERVING_PRESSURE_SUSTAIN);
    let mut host = pressured;
    let mut decision = Decision::default();
    assert_eq!(run(&mut host, &mut decision, start, 0..bound, false), None);
    assert_eq!(
        run(&mut host, &mut decision, start, bound..bound + 1, false),
        Some((bound, Trigger::SustainedPressure))
    );
    // A load at the same pressure is below the loading backstop.
    let mut loading = pressured;
    assert_eq!(
        run(
            &mut loading,
            &mut Decision::default(),
            start,
            0..ticks(Duration::from_secs(300)),
            true
        ),
        None
    );
    // Loading: avg60 above 80% and not falling, for 60 s.
    let mut stuck = Host {
        avg10: 85.0,
        avg60: 85.0,
        ..Host::serving()
    };
    let bound = ticks(LOADING_PRESSURE_SUSTAIN);
    let mut decision = Decision::default();
    assert_eq!(run(&mut stuck, &mut decision, start, 0..bound, true), None);
    assert_eq!(
        run(&mut stuck, &mut decision, start, bound..bound + 1, true),
        Some((bound, Trigger::SustainedPressure))
    );
    // Falling pressure (avg10 well below avg60) is a load recovering.
    let mut recovering = Host {
        avg10: 60.0,
        avg60: 85.0,
        ..Host::serving()
    };
    assert_eq!(
        run(
            &mut recovering,
            &mut Decision::default(),
            start,
            0..400,
            true
        ),
        None
    );
}

#[test]
fn r5_the_nvrm_line_kills_only_with_sustained_corroboration() {
    // Break caught: a healthy load's derived MemFree touches ~0.9 GB at its
    // NVRM line for one sample (GLM mosaic a0 2026-10-28); one sample never kills.
    let start = Instant::now();
    let roomy = Sample {
        available_bytes: Some(10 * GIB),
        free_bytes: Some(5 * GIB),
        swap_total_bytes: Some(64 * GIB),
        swap_free_bytes: Some(60 * GIB),
        full_avg10: Some(40.0),
        full_avg60: Some(20.0),
        counters: Some(Counters::default()),
    };
    let free_low = Sample {
        free_bytes: Some(DRIVER_FAILURE_FREE_BYTES - 1),
        ..roomy
    };
    let mut decision = Decision::default();
    // Once per normal load: never alone, and never with one low sample.
    assert_eq!(decision.observe(start, roomy, true, true, true), None);
    assert_eq!(
        decision.observe(start + SAMPLE_INTERVAL, free_low, true, true, true),
        None
    );
    assert_eq!(
        decision.observe(start + SAMPLE_INTERVAL * 2, roomy, true, true, true),
        None
    );
    // Sustained for the R1 window with the line: killed.
    let bound = ticks(DRIVER_FAILURE_SUSTAIN);
    for tick in 3..3 + bound {
        assert_eq!(
            decision.observe(start + SAMPLE_INTERVAL * tick, free_low, true, true, true),
            None
        );
    }
    assert_eq!(
        decision.observe(
            start + SAMPLE_INTERVAL * (3 + bound),
            free_low,
            true,
            true,
            true
        ),
        Some(Trigger::DriverAllocationFailure)
    );
    // The same sustained low MemFree without the line is no kill (healthy
    // loads run with little free memory).
    let mut quiet = Decision::default();
    for tick in 0..400 {
        assert_eq!(
            quiet.observe(start + SAMPLE_INTERVAL * tick, free_low, false, true, true),
            None
        );
    }
}

#[test]
fn unknown_readings_and_unmanaged_hosts_never_kill_and_reset_continuity() {
    let start = Instant::now();
    let exhausted = Sample {
        available_bytes: Some(1),
        free_bytes: Some(1),
        swap_total_bytes: Some(64 * GIB),
        swap_free_bytes: Some(0),
        full_avg10: Some(99.0),
        full_avg60: Some(99.0),
        counters: Some(Counters::default()),
    };
    let mut decision = Decision::default();
    for tick in 0..ticks(SWAP_EXHAUSTED_SUSTAIN) {
        assert_eq!(
            decision.observe(
                start + SAMPLE_INTERVAL * tick,
                exhausted,
                false,
                true,
                false
            ),
            None
        );
    }
    // One unknown sample breaks the condition's continuity.
    let unknown = Sample::default();
    let tick = ticks(SWAP_EXHAUSTED_SUSTAIN);
    assert_eq!(
        decision.observe(start + SAMPLE_INTERVAL * tick, unknown, true, true, false),
        None
    );
    assert_eq!(
        decision.observe(
            start + SAMPLE_INTERVAL * (tick + 1),
            exhausted,
            false,
            true,
            false
        ),
        None
    );
    // Nothing managed runs: never a victim, and no state carries over.
    for tick in 0..100 {
        assert_eq!(
            decision.observe(
                start + SAMPLE_INTERVAL * tick,
                exhausted,
                true,
                false,
                false
            ),
            None
        );
    }
}

#[test]
fn a_stalled_sampler_neither_hides_a_runaway_nor_invents_a_rate() {
    let start = Instant::now();
    let mut decision = Decision::default();
    let exhausted = Sample {
        available_bytes: Some(1),
        free_bytes: Some(2 * GIB),
        swap_total_bytes: Some(64 * GIB),
        swap_free_bytes: Some(0),
        full_avg10: Some(10.0),
        full_avg60: Some(10.0),
        counters: Some(Counters::default()),
    };
    // Thrash stalls the sampler itself: two samples five seconds apart are
    // still one continuous exhaustion.
    assert_eq!(decision.observe(start, exhausted, false, true, false), None);
    assert_eq!(
        decision.observe(
            start + Duration::from_secs(5),
            exhausted,
            false,
            true,
            false
        ),
        Some(Trigger::MemoryAndSwapExhausted)
    );
    // A rate is pages over the real gap, never a per-tick delta.
    let mut decision = Decision::default();
    let later = Sample {
        counters: Some(Counters {
            swap_in: 50_000,
            swap_out: 0,
            refault: 50_000,
        }),
        ..Sample::default()
    };
    decision.observe(start, Sample::default(), false, true, false);
    decision.observe(
        start + Duration::from_secs(1),
        Sample {
            counters: Some(Counters::default()),
            ..Sample::default()
        },
        false,
        true,
        false,
    );
    decision.observe(start + Duration::from_secs(11), later, false, true, false);
    let rates = decision.rates().unwrap();
    assert_eq!(rates.thrash(), 10_000.0);
}

#[test]
fn the_kernel_files_are_read_into_one_sample_and_the_evidence_names_swap() {
    let sample = read_sample(|path| {
        Ok(match path {
            "/proc/meminfo" => "MemTotal: 128 kB\nMemFree: 2048 kB\nMemAvailable: 4096 kB\nSwapTotal: 67108864 kB\nSwapFree: 33554432 kB\n".to_owned(),
            "/proc/pressure/memory" => "some avg10=80.00 avg60=70.00 avg300=1.00 total=1\nfull avg10=45.50 avg60=50.25 avg300=1.00 total=1\n".to_owned(),
            "/proc/vmstat" => "pswpin 10\npswpout 20\nworkingset_refault_anon 3\nworkingset_refault_file 4\n".to_owned(),
            other => panic!("unexpected {other}"),
        })
    });
    assert_eq!(sample.available_bytes, Some(4096 * 1024));
    assert_eq!(sample.free_bytes, Some(2048 * 1024));
    assert_eq!(sample.swap_total_bytes, Some(64 * GIB));
    assert_eq!(sample.swap_free_bytes, Some(32 * GIB));
    assert_eq!(sample.full_avg10, Some(45.5), "full, never some");
    assert_eq!(sample.full_avg60, Some(50.25));
    assert_eq!(
        sample.counters,
        Some(Counters {
            swap_in: 10,
            swap_out: 20,
            refault: 7
        })
    );
    let text = readings(
        &sample,
        Some(Rates {
            swap_in: 1.0,
            swap_out: 2.0,
            refault: 3.0,
        }),
        true,
    );
    assert_eq!(text.0, "loading");
    assert!(text.1.len() <= 256);
    for field in [
        "true ",
        "swap_total_mib=65536",
        "swap_free_mib=32768",
        "swapin_per_s=1",
        "swapout_per_s=2",
        "refault_per_s=3",
    ] {
        assert!(text.1.contains(field), "{field} in {text:?}");
    }
    assert!(
        readings(&Sample::default(), None, false)
            .1
            .contains(&format!(
                "swapin_per_s={}",
                ResourceTermProblem::Unknown.as_str()
            ))
    );
}

#[test]
fn a_phase_flip_starts_the_pressure_window_over() {
    // Break caught: 40 s of loading pressure read as 40 s of serving pressure
    // the moment one slow drop let the loading phase lapse.
    let mut host = Host {
        avg10: 85.0,
        avg60: 85.0,
        ..Host::serving()
    };
    let mut decision = Decision::default();
    let start = Instant::now();
    let loading = ticks(Duration::from_secs(40));
    assert_eq!(run(&mut host, &mut decision, start, 0..loading, true), None);
    let serving = loading + ticks(SERVING_PRESSURE_SUSTAIN);
    assert_eq!(
        run(&mut host, &mut decision, start, loading..serving, false),
        None
    );
    assert_eq!(
        run(&mut host, &mut decision, start, serving..serving + 1, false),
        Some((serving, Trigger::SustainedPressure))
    );
}

#[test]
fn unswappable_growth_with_swap_still_free_is_killed_by_the_pressure_backstop() {
    // GPU and pinned allocations never reach swap: memory runs out while swap
    // stays free, so only R4 (and R5 with the driver line) can act.
    let mut host = Host {
        available: 300 * 1024 * 1024,
        free: 200 * 1024 * 1024,
        avg10: 95.0,
        avg60: 90.0,
        ..Host::serving()
    };
    let start = Instant::now();
    let bound = ticks(SERVING_PRESSURE_SUSTAIN);
    assert_eq!(
        run(
            &mut host,
            &mut Decision::default(),
            start,
            0..bound + 1,
            false
        ),
        Some((bound, Trigger::SustainedPressure))
    );
    let bound = ticks(LOADING_PRESSURE_SUSTAIN);
    assert_eq!(
        run(
            &mut host,
            &mut Decision::default(),
            start,
            0..bound + 1,
            true
        ),
        Some((bound, Trigger::SustainedPressure))
    );
    // With the NVRM line and MemFree exhausted for the R1 window: killed.
    let mut decision = Decision::default();
    let bound = ticks(DRIVER_FAILURE_SUSTAIN);
    assert_eq!(
        run_with_line(&mut host, &mut decision, start, 0..bound + 1),
        Some((bound, Trigger::DriverAllocationFailure))
    );
}

#[test]
fn the_recorded_glm_mosaic_load_curve_is_never_killed() {
    // Hardware 2026-10-21 and 2026-10-28 (GLM 5.3 Flash mosaic, 7 false
    // kills under the old 30%/3 s/16 GiB rule): MemAvailable falls ~2 GB per
    // 5 s from 19.6 to ~13 GB with full avg10 55-67 for ~100 s while ~6 GB is
    // written to swap, then stays flat as the weights sit on the GPU.
    let mut host = Host {
        available: 19_600_000_000,
        free: 1_500_000_000,
        avg10: 60.0,
        avg60: 58.0,
        swap_out_per_s: 14_650,
        swap_in_per_s: 200,
        refault_per_s: 3_000,
        ..Host::serving()
    };
    let mut decision = Decision::default();
    let start = Instant::now();
    let falling = ticks(Duration::from_secs(100));
    for tick in 0..falling + ticks(Duration::from_secs(300)) {
        if tick < falling {
            host.available = 19_600_000_000_u64
                .saturating_sub(u64::from(tick) * 2_000_000_000 / 20)
                .max(13_000_000_000);
            host.avg10 = [55.0, 60.0, 67.0, 62.0][tick as usize % 4];
        } else {
            host.swap_out_per_s = 0;
            host.avg10 = 20.0;
            host.avg60 = 30.0;
        }
        let sample = host.tick();
        assert_eq!(
            decision.observe(
                start + SAMPLE_INTERVAL * tick,
                sample,
                tick % 40 == 0,
                true,
                true
            ),
            None,
            "tick {tick}: available {} avg10 {}",
            host.available,
            host.avg10
        );
    }
    let written = host.swap_total - host.swap_free;
    assert!((5 * GIB..7 * GIB).contains(&written), "{written}");
}

#[test]
fn the_kill_evidence_carries_the_recent_samples_oldest_first() {
    let mut host = Host::serving();
    let mut decision = Decision::default();
    let start = Instant::now();
    for tick in 0..ticks(Duration::from_secs(20)) {
        host.available = 20 * GIB - u64::from(tick) * (GIB / 40);
        let sample = host.tick();
        decision.observe(start + SAMPLE_INTERVAL * tick, sample, false, true, false);
    }
    let (name, value) = decision.recent();
    assert_eq!(name, RECENT_FIELDS[0]);
    assert!(value.len() <= 256);
    let available: Vec<u64> = value
        .split_whitespace()
        .next()
        .unwrap()
        .split(':')
        .map(|v| v.parse().unwrap())
        .collect();
    assert_eq!(available.len(), RECENT_SAMPLES);
    assert!(
        available.windows(2).all(|pair| pair[0] > pair[1]),
        "{available:?}"
    );
    for field in &RECENT_FIELDS[1..] {
        assert!(value.contains(&format!("{field}=")), "{field} in {value}");
    }
}
