use super::*;

#[test]
fn recorded_gb10_health_does_not_confuse_missing_metrics_with_reset() {
    // Wrong implementation: treats N/A as a failure or ERR! as unsupported.
    let healthy = include_bytes!("../../tests/fixtures/gpu-health-healthy.txt");
    let reset = include_bytes!("../../tests/fixtures/gpu-health-needs-reset.txt");
    assert_eq!(classify(true, healthy, b""), GpuHealth::Healthy);
    assert_eq!(classify(true, reset, b""), GpuHealth::NeedsReset);
    assert_eq!(classify(false, b"", reset), GpuHealth::NeedsReset);
    assert_eq!(
        classify(
            false,
            b"",
            b"NVIDIA-SMI couldn't communicate with the NVIDIA driver"
        ),
        GpuHealth::Unavailable
    );
    for evidence in [
        b"GPU 0: ERR!".as_slice(),
        b"Xid 119",
        b"NVRM: Xid (PCI:0000000F:01:00): 120, pid=13",
        b"Xid 154 GPU recovery action changed from 0x0 (None) to 0x1 (GPU Reset Required)",
        b"NV_ERR_GPU_IN_FULLCHIP_RESET",
        b"GPU Recovery Action : Reset",
    ] {
        assert_eq!(classify(false, evidence, b""), GpuHealth::NeedsReset);
    }
    for evidence in [
        "Xid 13",
        "Xid 94",
        "NVRM: Xid (PCI:00000000:79:00): 13, pid=119",
        "Xid counter: 119",
        "Xid 154 GPU recovery action changed from 0x1 (GPU Reset Required) to 0x0 (None)",
    ] {
        let observation = format!("{}\n{evidence}", String::from_utf8_lossy(healthy));
        assert_eq!(
            classify(true, observation.as_bytes(), b""),
            GpuHealth::Healthy
        );
    }
    // Later observation replaces the fault without any persistent flag.
    assert_eq!(classify(true, healthy, b""), GpuHealth::Healthy);
}
