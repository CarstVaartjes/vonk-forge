#![forbid(unsafe_code)]

use std::{
    cell::{Cell, RefCell},
    fs,
    path::Path,
    time::Duration,
};

use tempfile::tempdir;
use vonk_agent::{
    inventory::{InventoryCollector, available_memory_bytes},
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
};

struct FakeRunner {
    calls: RefCell<Vec<(Program, Vec<String>)>>,
    gpu_output: &'static [u8],
    cdi_output: &'static [u8],
}

impl ProcessRunner for FakeRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        _timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.calls.borrow_mut().push((program, arguments.to_vec()));
        let stdout = match program {
            Program::NvidiaSmi => self.gpu_output.to_vec(),
            Program::Podman => b"5.4.2\n".to_vec(),
            Program::Docker => b"Docker version 29.2.1, build test\n".to_vec(),
            Program::NvidiaCtk => self.cdi_output.to_vec(),
            _ => unreachable!(),
        };
        Ok(ProcessOutput {
            success: true,
            stdout,
            stderr: vec![],
        })
    }
}

struct DelayedReadyRunner {
    inner: FakeRunner,
    failures_remaining: Cell<u32>,
}

impl ProcessRunner for DelayedReadyRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        let failures = self.failures_remaining.get();
        if failures > 0 {
            self.failures_remaining.set(failures - 1);
            return Ok(ProcessOutput {
                success: false,
                stdout: Vec::new(),
                stderr: Vec::new(),
            });
        }
        self.inner.run(program, arguments, timeout)
    }
}

#[test]
fn inventory_reports_physical_and_available_memory_disk_and_gpu() {
    let directory = tempdir().unwrap();
    let meminfo = directory.path().join("meminfo");
    fs::write(
        &meminfo,
        "MemTotal:       123456 kB\nMemAvailable:    65432 kB\n",
    )
    .unwrap();
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        gpu_output: b"NVIDIA H100, 119808, 110000, 590.44\n",
        cdi_output: b"nvidia.com/gpu=all\n",
    };
    let inventory = InventoryCollector {
        runner: &runner,
        meminfo_path: &meminfo,
        store_path: directory.path(),
        egress_binary_path: Path::new("/bin/true"),
        fabric_address: Some("192.168.100.2".parse().unwrap()),
        fabric_bandwidth_mbps: Some(200_000),
    }
    .collect()
    .unwrap();

    assert_eq!(inventory.memory_total_bytes, 123456 * 1024);
    assert_eq!(inventory.memory_available_bytes, 65432 * 1024);
    assert!(inventory.disk_total_bytes >= inventory.disk_available_bytes);
    assert_eq!(inventory.gpu_count, 1);
    assert_eq!(inventory.gpu_memory_total_bytes, 119808 * 1024 * 1024);
    assert_eq!(inventory.gpu_memory_free_bytes, 110000 * 1024 * 1024);
    assert_eq!(
        inventory.fabric_address.unwrap().to_string(),
        "192.168.100.2"
    );
    assert!(
        inventory
            .capabilities
            .contains(&"runtime.vonk.v1".to_owned())
    );
    assert!(
        inventory
            .capabilities
            .contains(&"build.rootless-podman.v1".to_owned())
    );
    assert!(
        inventory
            .capabilities
            .contains(&"recipe.build.egress-proxy.v1".to_owned())
    );
    assert!(
        inventory
            .capabilities
            .contains(&"runtime.spark-docker-nvidia.v1".to_owned())
    );
    assert!(
        inventory
            .capabilities
            .contains(&"fabric.connected.mbps.200000".to_owned())
    );
    assert_eq!(runner.calls.borrow().len(), 4);
    assert_eq!(
        available_memory_bytes(&runner, &meminfo, "unified").unwrap(),
        65432 * 1024
    );
    let wire = serde_json::to_value(inventory.to_request(chrono::Utc::now().into())).unwrap();
    assert_eq!(wire["memory_pool"], "separate");
    assert_eq!(wire["host_memory_total_bytes"], 123456 * 1024);
    assert_eq!(wire["host_memory_free_bytes"], 65432 * 1024);
    assert_eq!(wire["disk_free_bytes"], inventory.disk_available_bytes);
    assert!(wire.get("memory_total_bytes").is_none());
    assert!(wire.get("memory_available_bytes").is_none());
    assert!(wire.get("disk_available_bytes").is_none());
    assert!(wire.get("state_database_reserve_held").is_none());
}

#[test]
fn malformed_gpu_row_does_not_hide_other_inventory_devices() {
    let directory = tempdir().unwrap();
    let meminfo = directory.path().join("meminfo");
    fs::write(
        &meminfo,
        "MemTotal:       123456 kB\nMemAvailable:    65432 kB\n",
    )
    .unwrap();
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        gpu_output: b"malformed device row\nNVIDIA H100, 119808, 110000, 590.44\n",
        cdi_output: b"nvidia.com/gpu=all\n",
    };
    let inventory = InventoryCollector {
        runner: &runner,
        meminfo_path: &meminfo,
        store_path: directory.path(),
        egress_binary_path: Path::new("/bin/true"),
        fabric_address: None,
        fabric_bandwidth_mbps: None,
    }
    .collect()
    .unwrap();

    assert_eq!(inventory.gpu_count, 1);
    assert_eq!(inventory.gpu_memory_total_bytes, 119808 * 1024 * 1024);
    assert_eq!(inventory.gpu_memory_free_bytes, 110000 * 1024 * 1024);
}

#[test]
fn malformed_or_inconsistent_memory_evidence_fails_closed() {
    let directory = tempdir().unwrap();
    let meminfo = directory.path().join("meminfo");
    fs::write(&meminfo, "MemTotal: 1 kB\nMemAvailable: 2 kB\n").unwrap();
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        gpu_output: b"NVIDIA H100, 119808, 110000, 590.44\n",
        cdi_output: b"nvidia.com/gpu=all\n",
    };
    assert!(
        InventoryCollector {
            runner: &runner,
            meminfo_path: &meminfo,
            store_path: directory.path(),
            egress_binary_path: Path::new("/bin/true"),
            fabric_address: None,
            fabric_bandwidth_mbps: None,
        }
        .collect()
        .is_err()
    );
    assert!(runner.calls.borrow().is_empty());
}

#[test]
fn inventory_uses_host_memory_for_the_unified_memory_gb10() {
    let directory = tempdir().unwrap();
    let meminfo = directory.path().join("meminfo");
    fs::write(
        &meminfo,
        "MemTotal:       123456 kB\nMemAvailable:    65432 kB\n",
    )
    .unwrap();
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        gpu_output: b"NVIDIA GB10, [N/A], [N/A], 580.173.02\n",
        cdi_output: b"nvidia.com/gpu=all\n",
    };

    let inventory = InventoryCollector {
        runner: &runner,
        meminfo_path: &meminfo,
        store_path: directory.path(),
        egress_binary_path: Path::new("/bin/true"),
        fabric_address: None,
        fabric_bandwidth_mbps: None,
    }
    .collect()
    .unwrap();

    assert_eq!(inventory.gpu_count, 1);
    assert_eq!(inventory.gpu_memory_total_bytes, 123456 * 1024);
    assert_eq!(inventory.gpu_memory_free_bytes, 65432 * 1024);
    assert_eq!(
        serde_json::to_value(inventory.to_request(chrono::Utc::now().into())).unwrap()["memory_pool"],
        "shared"
    );
    assert_eq!(
        available_memory_bytes(&runner, &meminfo, "unified").unwrap(),
        65432 * 1024
    );
}

#[test]
fn numeric_gpu_counters_do_not_turn_gb10_into_a_dedicated_pool() {
    let directory = tempdir().unwrap();
    let meminfo = directory.path().join("meminfo");
    fs::write(&meminfo, "MemTotal: 123456 kB\nMemAvailable: 65432 kB\n").unwrap();
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        gpu_output: b"NVIDIA GB10, 120, 90, 580.173.02\n",
        cdi_output: b"nvidia.com/gpu=all\n",
    };
    let inventory = InventoryCollector {
        runner: &runner,
        meminfo_path: &meminfo,
        store_path: directory.path(),
        egress_binary_path: Path::new("/bin/true"),
        fabric_address: None,
        fabric_bandwidth_mbps: None,
    }
    .collect()
    .unwrap();
    assert_eq!(
        serde_json::to_value(inventory.to_request(chrono::Utc::now().into())).unwrap()["memory_pool"],
        "shared"
    );
    assert_eq!(inventory.gpu_memory_free_bytes, 65432 * 1024);
}

#[test]
fn unavailable_memory_on_an_unknown_gpu_fails_closed() {
    let directory = tempdir().unwrap();
    let meminfo = directory.path().join("meminfo");
    fs::write(
        &meminfo,
        "MemTotal:       123456 kB\nMemAvailable:    65432 kB\n",
    )
    .unwrap();
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        gpu_output: b"Unknown GPU, [N/A], [N/A], 580.173.02\n",
        cdi_output: b"nvidia.com/gpu=all\n",
    };

    assert!(
        InventoryCollector {
            runner: &runner,
            meminfo_path: &meminfo,
            store_path: directory.path(),
            egress_binary_path: Path::new("/bin/true"),
            fabric_address: None,
            fabric_bandwidth_mbps: None,
        }
        .collect()
        .is_err()
    );
}

#[test]
fn inventory_refuses_to_advertise_spark_runtime_without_nvidia_cdi() {
    let directory = tempdir().unwrap();
    let meminfo = directory.path().join("meminfo");
    fs::write(
        &meminfo,
        "MemTotal:       123456 kB\nMemAvailable:    65432 kB\n",
    )
    .unwrap();
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        gpu_output: b"NVIDIA GB10, [N/A], [N/A], 580.173.02\n",
        cdi_output: b"",
    };

    let collector = InventoryCollector {
        runner: &runner,
        meminfo_path: &meminfo,
        store_path: directory.path(),
        egress_binary_path: Path::new("/bin/true"),
        fabric_address: None,
        fabric_bandwidth_mbps: None,
    };
    assert!(collector.collect().is_err());
    let ready = FakeRunner {
        calls: RefCell::new(vec![]),
        cdi_output: b"nvidia.com/gpu=all\n",
        ..runner
    };
    assert_eq!(
        InventoryCollector {
            runner: &ready,
            meminfo_path: &meminfo,
            store_path: directory.path(),
            egress_binary_path: Path::new("/bin/true"),
            fabric_address: None,
            fabric_bandwidth_mbps: None
        }
        .collect()
        .unwrap()
        .gpu_count,
        1
    );
}

#[test]
fn transient_host_runtime_failures_remain_retryable_past_the_systemd_start_limit() {
    let directory = tempdir().unwrap();
    let meminfo = directory.path().join("meminfo");
    fs::write(
        &meminfo,
        "MemTotal:       123456 kB\nMemAvailable:    65432 kB\n",
    )
    .unwrap();
    let runner = DelayedReadyRunner {
        inner: FakeRunner {
            calls: RefCell::new(vec![]),
            gpu_output: b"NVIDIA H100, 119808, 110000, 590.44\n",
            cdi_output: b"nvidia.com/gpu=all\n",
        },
        failures_remaining: Cell::new(7),
    };
    let collector = InventoryCollector {
        runner: &runner,
        meminfo_path: &meminfo,
        store_path: directory.path(),
        egress_binary_path: Path::new("/bin/true"),
        fabric_address: None,
        fabric_bandwidth_mbps: None,
    };

    for _ in 0..7 {
        assert!(collector.collect().is_err());
    }
    assert_eq!(collector.collect().unwrap().gpu_count, 1);
}
