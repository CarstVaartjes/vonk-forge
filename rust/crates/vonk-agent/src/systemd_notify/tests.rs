#![cfg(test)]
use super::{combined_status, progress_payload, watchdog_enabled_for};

#[test]
fn other_lane_progress_cannot_hide_renewal_failure() {
    // Wrong implementation: inventory/control progress overwrites renewal health.
    let renewal = "Certificate renewal refused; re-enrollment needed";
    assert!(combined_status("Inventory progressing", Some(renewal)).contains(renewal));
    assert_eq!(
        combined_status("Control progressing", None),
        "Control progressing"
    );
}

#[test]
fn watchdog_is_enabled_only_for_a_valid_current_process() {
    assert!(watchdog_enabled_for(Some("1000000"), None, 42));
    assert!(watchdog_enabled_for(Some("1000000"), Some("42"), 42));
    assert!(!watchdog_enabled_for(Some("1000000"), Some("1"), 42));
    assert!(!watchdog_enabled_for(Some("invalid"), None, 42));
    assert!(!watchdog_enabled_for(Some("0"), None, 42));
}

#[test]
fn progress_pings_watchdog_only_when_service_manager_owns_this_process() {
    assert_eq!(
        progress_payload("loop progressed", true),
        "STATUS=loop progressed\nWATCHDOG=1"
    );
    assert_eq!(
        progress_payload("loop progressed", false),
        "STATUS=loop progressed"
    );
}
