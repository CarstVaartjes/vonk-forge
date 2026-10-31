//! Policy R1-R5 over one host sample; every threshold lives in
//! `vonk_agent_protocol::host_memory_guard_policy`.
use std::collections::VecDeque;
use std::time::{Duration, Instant};

use vonk_agent_protocol::failure_evidence::FAILURE_PROPERTY_VALUE_CHARS;
use vonk_agent_protocol::generated::ResourceTermProblem;

use vonk_agent_protocol::host_memory_guard_policy::{
    DRIVER_FAILURE_FREE_BYTES, DRIVER_FAILURE_RECLAIM_PAGES_PER_SECOND, DRIVER_FAILURE_SUSTAIN,
    DRIVER_LINE_WINDOW, EXHAUSTED_BYTES, LOADING_FULL_AVG60_PERCENT, LOADING_PRESSURE_SUSTAIN,
    PRESSURE_FIELDS, PRESSURE_HEADROOM_BYTES, PRESSURE_PROPERTY, PSI_FALLING_RATIO, RECENT_FIELDS,
    RECENT_PROPERTY, RECENT_SAMPLE_SPACING, RECENT_SAMPLES, SERVING_FULL_PSI_PERCENT,
    SERVING_PRESSURE_SUSTAIN, SUSTAINED_PRESSURE, SWAP_EXHAUSTED_FREE_FLOOR_BYTES,
    SWAP_EXHAUSTED_FREE_PERCENT, SWAP_EXHAUSTED_MEMORY_BYTES, SWAP_EXHAUSTED_SUSTAIN,
    SWAP_FILLING_MEMORY_BYTES, SWAP_FILLING_SUSTAIN, SWAP_FILLING_USED_PERCENT,
    THRASH_FULL_PSI_PERCENT, THRASH_PAGES_PER_SECOND, THRASH_SUSTAIN,
};

/// Why the guard stopped the managed workloads; the evidence's `trigger`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Trigger {
    /// R1 without swap: MemAvailable exhausted.
    MemoryExhausted,
    /// R1 with swap: MemAvailable and free swap both exhausted.
    MemoryAndSwapExhausted,
    /// R2: swap nearly full and still filling with memory low.
    SwapFilling,
    /// R3: swap-ins and refaults storm under pressure with no headroom left.
    Thrashing,
    /// R4: the PSI backstop for the phase (serving or loading).
    SustainedPressure,
    /// R5: an NVRM allocation failure with corroborating exhaustion.
    DriverAllocationFailure,
}

impl Trigger {
    pub const ALL: [Self; 6] = [
        Self::MemoryExhausted,
        Self::MemoryAndSwapExhausted,
        Self::SwapFilling,
        Self::Thrashing,
        Self::SustainedPressure,
        Self::DriverAllocationFailure,
    ];

    /// The evidence token. `trigger=<token>` stays under the sanitizers'
    /// 40-character opaque-value run, which would redact it whole.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::MemoryExhausted => "memavailable_exhausted",
            Self::MemoryAndSwapExhausted => "memory_and_swap_exhausted",
            Self::SwapFilling => "swap_nearly_full_and_filling",
            Self::Thrashing => "swap_and_refault_thrashing",
            Self::SustainedPressure => "sustained_full_psi",
            Self::DriverAllocationFailure => "nvidia_allocation_failure",
        }
    }
}

/// Cumulative `/proc/vmstat` page counters.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Counters {
    pub swap_in: u64,
    pub swap_out: u64,
    /// workingset_refault_anon + workingset_refault_file.
    pub refault: u64,
}

/// Swap-backed memory resident in RAM, recorded in kill evidence so a rule on
/// it can be calibrated on real readings; no rule reads it yet.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Resident {
    pub anon_bytes: Option<u64>,
    pub shmem_bytes: Option<u64>,
    pub active_anon_bytes: Option<u64>,
    pub inactive_anon_bytes: Option<u64>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Sample {
    pub available_bytes: Option<u64>,
    pub free_bytes: Option<u64>,
    pub swap_total_bytes: Option<u64>,
    pub swap_free_bytes: Option<u64>,
    pub resident: Resident,
    pub full_avg10: Option<f64>,
    pub full_avg60: Option<f64>,
    pub counters: Option<Counters>,
}

/// Page rates per second between two samples.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Rates {
    pub swap_in: f64,
    pub swap_out: f64,
    pub refault: f64,
}

impl Rates {
    pub fn thrash(&self) -> f64 {
        self.swap_in + self.refault
    }
}

/// One `/proc/meminfo` field (`key` with its colon) in bytes.
pub fn meminfo_bytes(text: &str, key: &str) -> Option<u64> {
    text.lines().find_map(|line| {
        let mut fields = line.split_whitespace();
        (fields.next()? == key).then(|| fields.next()?.parse::<u64>().ok()?.checked_mul(1024))?
    })
}

fn full_pressure(text: &str, window: &str) -> Option<f64> {
    text.lines().find_map(|line| {
        let mut fields = line.split_whitespace();
        if fields.next()? != "full" {
            return None;
        }
        let value = fields
            .find_map(|field| field.strip_prefix(window))?
            .parse::<f64>()
            .ok()?;
        (value.is_finite() && (0.0..=100.0).contains(&value)).then_some(value)
    })
}

fn vmstat(text: &str, key: &str) -> Option<u64> {
    text.lines().find_map(|line| {
        let mut fields = line.split_whitespace();
        (fields.next()? == key).then(|| fields.next()?.parse().ok())?
    })
}

pub fn read_sample(mut read: impl FnMut(&str) -> std::io::Result<String>) -> Sample {
    // An unreadable file leaves its readings unknown; unknown never kills.
    let meminfo = read("/proc/meminfo");
    let field = |key| {
        meminfo
            .as_deref()
            .ok()
            .and_then(|text| meminfo_bytes(text, key))
    };
    let pressure = read("/proc/pressure/memory");
    let full = |window| {
        pressure
            .as_deref()
            .ok()
            .and_then(|text| full_pressure(text, window))
    };
    let counters = read("/proc/vmstat").ok().and_then(|text| {
        Some(Counters {
            swap_in: vmstat(&text, "pswpin")?,
            swap_out: vmstat(&text, "pswpout")?,
            refault: vmstat(&text, "workingset_refault_anon")?
                .checked_add(vmstat(&text, "workingset_refault_file")?)?,
        })
    });
    Sample {
        available_bytes: field("MemAvailable:"),
        free_bytes: field("MemFree:"),
        swap_total_bytes: field("SwapTotal:"),
        swap_free_bytes: field("SwapFree:"),
        resident: Resident {
            anon_bytes: field("AnonPages:"),
            shmem_bytes: field("Shmem:"),
            active_anon_bytes: field("Active(anon):"),
            inactive_anon_bytes: field("Inactive(anon):"),
        },
        full_avg10: full("avg10="),
        full_avg60: full("avg60="),
        counters,
    }
}

/// Whether an NVRM line seen at `line` still counts at `now` (R5).
pub fn driver_line_counts(line: Instant, now: Instant) -> bool {
    now.saturating_duration_since(line) <= DRIVER_LINE_WINDOW
}

/// Each rule's condition, evaluated on one sample. Unknown readings never
/// satisfy a condition.
#[derive(Clone, Copy, Debug, Default)]
struct Conditions {
    swap: bool,
    exhausted: bool,
    filling: bool,
    thrashing: bool,
    pressure: bool,
    driver_free_low: bool,
}

fn conditions(sample: &Sample, rates: Option<Rates>, loading: bool) -> Conditions {
    let available = sample.available_bytes;
    let below = |value: Option<u64>, bar: u64| value.is_some_and(|value| value < bar);
    let swap_total = sample.swap_total_bytes.filter(|total| *total > 0);
    let swap = swap_total.is_some() && sample.swap_free_bytes.is_some();
    let exhausted = match (swap_total, sample.swap_free_bytes) {
        (Some(total), Some(free)) => {
            below(available, SWAP_EXHAUSTED_MEMORY_BYTES)
                && free
                    < SWAP_EXHAUSTED_FREE_FLOOR_BYTES.max(total / 100 * SWAP_EXHAUSTED_FREE_PERCENT)
        }
        _ => below(available, EXHAUSTED_BYTES),
    };
    let filling = match (swap_total, sample.swap_free_bytes, rates) {
        (Some(total), Some(free), Some(rates)) => {
            total.saturating_sub(free) > total / 100 * SWAP_FILLING_USED_PERCENT
                && rates.swap_out > 0.0
                && below(available, SWAP_FILLING_MEMORY_BYTES)
        }
        _ => false,
    };
    let headroom = available.map(|bytes| bytes.saturating_add(sample.swap_free_bytes.unwrap_or(0)));
    let low_headroom = below(headroom, PRESSURE_HEADROOM_BYTES);
    let thrashing = rates.is_some_and(|rates| rates.thrash() >= THRASH_PAGES_PER_SECOND as f64)
        && sample
            .full_avg10
            .is_some_and(|psi| psi > THRASH_FULL_PSI_PERCENT)
        && low_headroom;
    let pressure = low_headroom
        && if loading {
            match (sample.full_avg60, sample.full_avg10) {
                (Some(avg60), Some(avg10)) => {
                    avg60 > LOADING_FULL_AVG60_PERCENT && avg10 >= avg60 * PSI_FALLING_RATIO
                }
                _ => false,
            }
        } else {
            sample
                .full_avg10
                .is_some_and(|psi| psi > SERVING_FULL_PSI_PERCENT)
        };
    Conditions {
        swap,
        exhausted,
        filling,
        thrashing,
        pressure,
        // With swap, low MemFree while pages are swapping out is reclaim
        // absorbing the load; it counts only once swap-out has stopped.
        driver_free_low: below(sample.free_bytes, DRIVER_FAILURE_FREE_BYTES)
            && (!swap
                || rates
                    .is_some_and(|rates| rates.swap_out < DRIVER_FAILURE_RECLAIM_PAGES_PER_SECOND)),
    }
}

#[derive(Default)]
pub struct Decision {
    exhausted_since: Option<Instant>,
    filling_since: Option<Instant>,
    thrashing_since: Option<Instant>,
    pressure_since: Option<Instant>,
    // The phase the pressure window was measured in; a flip starts it over.
    pressure_loading: bool,
    corroborated_since: Option<Instant>,
    previous: Option<(Instant, Counters)>,
    rates: Option<Rates>,
    recent: VecDeque<(Instant, Sample)>,
}

impl Decision {
    /// The page rates between the last two samples, for evidence.
    pub fn rates(&self) -> Option<Rates> {
        self.rates
    }

    fn update_rates(&mut self, now: Instant, counters: Option<Counters>) {
        self.rates = match (self.previous, counters) {
            (Some((then, before)), Some(after)) if now > then => {
                let seconds = now.duration_since(then).as_secs_f64();
                let rate = |a: u64, b: u64| a.saturating_sub(b) as f64 / seconds;
                Some(Rates {
                    swap_in: rate(after.swap_in, before.swap_in),
                    swap_out: rate(after.swap_out, before.swap_out),
                    refault: rate(after.refault, before.refault),
                })
            }
            _ => None,
        };
        self.previous = counters.map(|counters| (now, counters));
    }

    pub fn observe(
        &mut self,
        now: Instant,
        sample: Sample,
        driver_failure: bool,
        managed: bool,
        loading: bool,
    ) -> Option<Trigger> {
        // A sampler stall is no reset: thrash stalls the sampler itself. A
        // condition stays continuous only while every observed sample shows it.
        if !managed {
            *self = Self::default();
            return None;
        }
        self.update_rates(now, sample.counters);
        if self
            .recent
            .back()
            .is_none_or(|(then, _)| now.saturating_duration_since(*then) >= RECENT_SAMPLE_SPACING)
        {
            if self.recent.len() == RECENT_SAMPLES {
                self.recent.pop_front();
            }
            self.recent.push_back((now, sample));
        }
        if loading != self.pressure_loading {
            self.pressure_loading = loading;
            self.pressure_since = None;
        }
        let condition = conditions(&sample, self.rates, loading);
        let sustained = |since: &mut Option<Instant>, active: bool, bound: Duration| {
            if !active {
                *since = None;
                return false;
            }
            now.duration_since(*since.get_or_insert(now)) >= bound
        };
        let exhausted = sustained(
            &mut self.exhausted_since,
            condition.exhausted,
            if condition.swap {
                SWAP_EXHAUSTED_SUSTAIN
            } else {
                SUSTAINED_PRESSURE
            },
        );
        let filling = sustained(
            &mut self.filling_since,
            condition.filling,
            SWAP_FILLING_SUSTAIN,
        );
        let thrashing = sustained(
            &mut self.thrashing_since,
            condition.thrashing,
            THRASH_SUSTAIN,
        );
        let pressure = sustained(
            &mut self.pressure_since,
            condition.pressure,
            if loading {
                LOADING_PRESSURE_SUSTAIN
            } else {
                SERVING_PRESSURE_SUSTAIN
            },
        );
        // R5: the line alone is non-fatal on GB10; it counts only with R1's or
        // R3's condition, or MemFree exhausted while no swap-out is reclaiming
        // (always, without swap), sustained like R1.
        let corroborated = sustained(
            &mut self.corroborated_since,
            condition.exhausted || condition.thrashing || condition.driver_free_low,
            DRIVER_FAILURE_SUSTAIN,
        );
        if driver_failure && corroborated {
            Some(Trigger::DriverAllocationFailure)
        } else if exhausted {
            Some(if condition.swap {
                Trigger::MemoryAndSwapExhausted
            } else {
                Trigger::MemoryExhausted
            })
        } else if filling {
            Some(Trigger::SwapFilling)
        } else if thrashing {
            Some(Trigger::Thrashing)
        } else if pressure {
            Some(Trigger::SustainedPressure)
        } else {
            None
        }
    }

    pub fn pressure_duration(&self, now: Instant, trigger: Trigger) -> Duration {
        let since = match trigger {
            Trigger::MemoryExhausted | Trigger::MemoryAndSwapExhausted => self.exhausted_since,
            Trigger::SwapFilling => self.filling_since,
            Trigger::Thrashing => self.thrashing_since,
            Trigger::SustainedPressure => self.pressure_since,
            Trigger::DriverAllocationFailure => self.corroborated_since,
        };
        since.map_or(Duration::ZERO, |since| now.duration_since(since))
    }
}

fn unknown() -> String {
    ResourceTermProblem::Unknown.as_str().to_owned()
}

fn mib(value: Option<u64>) -> String {
    value.map_or_else(unknown, |bytes| (bytes >> 20).to_string())
}

/// One packed evidence value: `field:reading` pairs joined by `+`, a single
/// summary token the agent keeps whole as one typed exit fact.
fn packed(fields: &[&str], readings: &[String]) -> String {
    let text = fields
        .iter()
        .zip(readings)
        .map(|(field, reading)| format!("{field}:{reading}"))
        .collect::<Vec<_>>()
        .join("+");
    text.chars().take(FAILURE_PROPERTY_VALUE_CHARS).collect()
}

/// The readings behind a kill (Step 0) as one evidence property,
/// `PRESSURE_PROPERTY`, its value packing every `PRESSURE_FIELDS` reading.
pub fn readings(sample: &Sample, rates: Option<Rates>, loading: bool) -> (String, String) {
    let rate = |value: Option<f64>| value.map_or_else(unknown, |v| format!("{v:.0}"));
    let values = [
        loading.to_string(),
        mib(sample.free_bytes),
        mib(sample.swap_total_bytes),
        mib(sample.swap_free_bytes),
        mib(sample.resident.anon_bytes),
        mib(sample.resident.shmem_bytes),
        mib(sample.resident.active_anon_bytes),
        mib(sample.resident.inactive_anon_bytes),
        sample.full_avg60.map_or_else(unknown, |v| v.to_string()),
        rate(rates.map(|r| r.swap_in)),
        rate(rates.map(|r| r.swap_out)),
        rate(rates.map(|r| r.refault)),
    ];
    (
        PRESSURE_PROPERTY.to_owned(),
        packed(&PRESSURE_FIELDS, &values),
    )
}

impl Decision {
    /// The last samples, one per `RECENT_SAMPLE_SPACING`, oldest first, as one
    /// evidence property, `RECENT_PROPERTY`: each `RECENT_FIELDS` series is its
    /// readings joined by `:`.
    pub fn recent(&self) -> (String, String) {
        let series = |reading: &dyn Fn(&Sample) -> String| {
            self.recent
                .iter()
                .map(|(_, sample)| reading(sample))
                .collect::<Vec<_>>()
                // `:` bounds every run below the sanitizers' 40-character
                // opaque-value rule (`-` and `+` would join one long run).
                .join(":")
        };
        let columns = [
            series(&|s| mib(s.available_bytes)),
            series(&|s| mib(s.free_bytes)),
            series(&|s| mib(s.swap_free_bytes)),
            series(&|s| s.full_avg10.map_or_else(unknown, |v| format!("{v:.0}"))),
        ];
        (RECENT_PROPERTY.to_owned(), packed(&RECENT_FIELDS, &columns))
    }
}

#[cfg(test)]
mod tests;
