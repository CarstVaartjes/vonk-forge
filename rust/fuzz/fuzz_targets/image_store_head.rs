//! Invariant: parsing never panics, and an accepted head names a servable path.
#![no_main]

use libfuzzer_sys::fuzz_target;
use vonk_agent::image_store::{parse_request_head, valid_store_path};

fuzz_target!(|data: &[u8]| {
    if let Ok(request) = parse_request_head(data) {
        assert!(valid_store_path(&request.path), "{}", request.path);
    }
});
