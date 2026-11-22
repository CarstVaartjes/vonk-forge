use super::*;

#[test]
fn probe_cleanup_follows_the_work_it_undoes_never_a_fixed_second() {
    // A quick probe leaves its cleanup the rest of the preflight budget.
    let now = Instant::now();
    assert!(cleanup_budget(now, now + Duration::from_secs(10)) > Duration::from_secs(9));
    // A probe that ran past the budget on a loaded host gets as long to
    // remove its objects as it took to make and run them.
    let slow = now.checked_sub(Duration::from_secs(12)).unwrap();
    assert!(cleanup_budget(slow, slow + Duration::from_secs(10)) >= Duration::from_secs(12));
}
#[test]
fn serving_probe_has_no_device_mount_capability_or_network_grants() {
    let args = run_arguments("probe-name", "probe-image");
    for flag in [
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--read-only",
        "--pull=never",
        "--user=65534:65534",
    ] {
        assert!(args.contains(&flag.to_owned()));
    }
    assert!(!args.iter().any(|arg| arg.starts_with("--cap-add")
        || arg.starts_with("--device")
        || arg.starts_with("--volume")
        || arg == "--privileged"
        || arg == "--pid=host"));
}
