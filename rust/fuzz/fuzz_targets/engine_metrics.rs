//! Invariant: parsing never panics, and every real number in a reading is
//! finite (the KV ratio is also within 0..=1).
#![no_main]

use libfuzzer_sys::fuzz_target;
use uuid::Uuid;
use vonk_agent::telemetry::engine_metrics::parse_engine;

fuzz_target!(|data: &[u8]| {
    let body = String::from_utf8_lossy(data);
    let Some(reading) = parse_engine(&body, Uuid::nil()) else {
        return;
    };
    for seconds in [
        reading.ttft_seconds_sum,
        reading.time_per_output_token_seconds_sum,
        reading.prefill_seconds_sum,
        reading.decode_seconds_sum,
        reading.e2e_seconds_sum,
    ]
    .into_iter()
    .flatten()
    {
        assert!(seconds.is_finite(), "{seconds}");
    }
    if let Some(ratio) = reading.kv_cache_usage_ratio {
        assert!((0.0..=1.0).contains(&ratio), "{ratio}");
    }
});
