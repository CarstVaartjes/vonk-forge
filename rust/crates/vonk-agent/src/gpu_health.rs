//! GPU health is independent of support for individual GB10 metrics.
use std::time::Duration;

use vonk_agent_protocol::generated::GpuHealth;

use crate::process::{ProcessRunner, Program};

pub(crate) fn requires_reset(bytes: &[u8]) -> bool {
    let text = String::from_utf8_lossy(bytes).to_ascii_lowercase();
    text.contains("gpu requires reset")
        || text.contains("err!")
        || text.contains("nv_err_gpu_in_fullchip_reset")
        || text.lines().any(reset_xid)
        || text.lines().any(|line| {
            if let Some((_, change)) = line.split_once("gpu recovery action changed") {
                // Xid 154 names both old and new actions; only the target wins.
                return change
                    .rsplit_once(" to ")
                    .is_some_and(|(_, target)| reset_action(target));
            }
            line.contains("gpu reset required")
                || line.contains("node reboot required")
                || line.split_once(':').is_some_and(|(key, value)| {
                    key.trim() == "gpu recovery action" && reset_action(value)
                })
        })
}

fn reset_action(value: &str) -> bool {
    matches!(
        value.trim(),
        "reset" | "reboot" | "drain and reset" | "gpu reset required" | "node reboot required"
    ) || [
        "(drain and reset)",
        "(gpu reset required)",
        "(node reboot required)",
    ]
    .into_iter()
    .any(|action| value.trim_end().ends_with(action))
}

fn reset_xid(line: &str) -> bool {
    let Some((_, value)) = line.split_once("xid") else {
        return false;
    };
    let value = value.trim_start();
    let value = if value.starts_with('(') {
        let Some((_, suffix)) = value.split_once(')') else {
            return false;
        };
        suffix
    } else {
        value
    };
    let code = value
        .trim_start_matches(|c: char| c.is_whitespace() || c == ':')
        .split(|c: char| !c.is_ascii_digit())
        .next()
        .and_then(|code| code.parse::<u32>().ok());
    // NVIDIA's immediate RESET_GPU/RESTART_BM and required-reset workflows:
    // https://docs.nvidia.com/deploy/xid-errors/analyzing-xid-catalog.html
    // Application Xids (for example 13/31/43/94) do not prove a reset is needed.
    matches!(
        code,
        Some(
            46 | 48
                | 62
                | 64
                | 74
                | 79
                | 95
                | 109
                | 110
                | 119
                | 120
                | 136
                | 140
                | 143
                | 155
                | 156
                | 158
        )
    )
}

pub(crate) fn observe<R: ProcessRunner>(runner: &R) -> GpuHealth {
    match runner.run(
        Program::NvidiaSmi,
        &["-q".to_owned()],
        Duration::from_secs(10),
    ) {
        Ok(output) => classify(output.success, &output.stdout, &output.stderr),
        Err(error) => {
            eprintln!("vonk-agent: GPU health observation unavailable: {error}");
            GpuHealth::Unavailable
        }
    }
}

fn classify(success: bool, stdout: &[u8], stderr: &[u8]) -> GpuHealth {
    // A reset signature wins even when NVML exits nonzero. N/A alone never
    // proves a reset; GB10 legitimately lacks dedicated-memory/fan metrics.
    if requires_reset(stdout) || requires_reset(stderr) {
        return GpuHealth::NeedsReset;
    }
    let text = String::from_utf8_lossy(stdout);
    let live_state = text.lines().any(|line| {
        line.split_once(':').is_some_and(|(key, value)| {
            key.trim() == "Performance State"
                && value
                    .trim()
                    .strip_prefix('P')
                    .is_some_and(|state| state.parse::<u8>().is_ok_and(|state| state <= 15))
        })
    });
    if success && live_state {
        GpuHealth::Healthy
    } else {
        GpuHealth::Unavailable
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recorded_gb10_health_does_not_confuse_missing_metrics_with_reset() {
        // Wrong implementation: treats N/A as a failure or ERR! as unsupported.
        let healthy = include_bytes!("../tests/fixtures/gpu-health-healthy.txt");
        let reset = include_bytes!("../tests/fixtures/gpu-health-needs-reset.txt");
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
}
