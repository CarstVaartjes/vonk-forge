#![cfg(test)]
use std::fs;

use super::*;

fn vector() -> serde_json::Value {
    serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/telemetry-sample-v2.json"
    ))
    .unwrap()
}

fn put(path: PathBuf, text: &serde_json::Value) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text.as_str().unwrap()).unwrap();
}

/// Lay the vector's verbatim captures out as the kernel does.
fn kernel_tree(root: &Path, store: &Path) {
    let captures = &vector()["captures"];
    let (proc, sys) = (root.join("proc"), root.join("sys"));
    put(proc.join("stat"), &captures["proc_stat_cpu_line"]);
    put(proc.join("loadavg"), &captures["proc_loadavg"]);
    put(proc.join("vmstat"), &captures["proc_vmstat_oom_kill"]);
    put(proc.join("net/dev"), &captures["proc_net_dev"]);
    put(proc.join("net/route"), &captures["proc_net_route"]);
    let files = |directory: &serde_json::Value, base: PathBuf| {
        for (chip, entries) in directory.as_object().unwrap() {
            for (file, text) in entries.as_object().unwrap() {
                put(base.join(chip).join(file), text);
            }
        }
    };
    files(&captures["hwmon"], sys.join("class/hwmon"));
    for (device, entries) in captures["infiniband"].as_object().unwrap() {
        let port = sys.join("class/infiniband").join(device).join("ports/1");
        put(port.join("state"), &entries["state"]);
        for counter in ["port_rcv_data", "port_xmit_data"] {
            put(port.join("counters").join(counter), &entries[counter]);
        }
    }
    // The store lives on a partition of the whole disk nvme0n1.
    let disk = sys.join("block/nvme0n1");
    put(disk.join("stat"), &captures["block_stat_nvme0n1"]);
    fs::create_dir_all(disk.join("nvme0n1p2")).unwrap();
    fs::write(disk.join("nvme0n1p2/partition"), "2\n").unwrap();
    let device = rustix::fs::stat(store).unwrap().st_dev;
    fs::create_dir_all(sys.join("dev/block")).unwrap();
    std::os::unix::fs::symlink(
        disk.join("nvme0n1p2"),
        sys.join(format!(
            "dev/block/{}:{}",
            rustix::fs::major(device),
            rustix::fs::minor(device)
        )),
    )
    .unwrap();
}

fn roots(root: &Path) -> HostRoots {
    HostRoots {
        proc: root.join("proc"),
        sys: root.join("sys"),
    }
}

#[test]
fn the_captured_spark_files_give_the_vector_values() {
    let directory = tempfile::tempdir().unwrap();
    kernel_tree(directory.path(), directory.path());
    let expected = &vector()["sample"];
    let read = read_host(&roots(directory.path()), directory.path());
    let want = |name: &str| expected[name].clone();
    assert_eq!(
        serde_json::json!(read.cpu_busy_seconds),
        want("cpu_busy_seconds")
    );
    assert_eq!(
        serde_json::json!(read.cpu_total_seconds),
        want("cpu_total_seconds")
    );
    assert_eq!(
        serde_json::json!(read.load_average_1m),
        want("load_average_1m")
    );
    assert_eq!(
        serde_json::json!(read.network_lan_rx_bytes),
        want("network_lan_rx_bytes")
    );
    assert_eq!(
        serde_json::json!(read.network_lan_tx_bytes),
        want("network_lan_tx_bytes")
    );
    assert_eq!(
        serde_json::json!(read.fabric_rx_bytes),
        want("fabric_rx_bytes")
    );
    assert_eq!(
        serde_json::json!(read.fabric_tx_bytes),
        want("fabric_tx_bytes")
    );
    assert_eq!(
        serde_json::json!(read.fabric_ports_active),
        want("fabric_ports_active")
    );
    assert_eq!(
        serde_json::json!(read.disk_read_bytes),
        want("disk_read_bytes")
    );
    assert_eq!(
        serde_json::json!(read.disk_written_bytes),
        want("disk_written_bytes")
    );
    assert_eq!(serde_json::json!(read.oom_kills), want("oom_kills"));
    assert_eq!(
        serde_json::json!(read.board_temperature_max_c),
        want("board_temperature_max_c")
    );
    assert_eq!(
        serde_json::json!(read.nvme_temperature_c),
        want("nvme_temperature_c")
    );
    assert_eq!(
        serde_json::json!(read.nic_temperature_c),
        want("nic_temperature_c")
    );
    assert_eq!(serde_json::json!(read.fan_rpm_max), want("fan_rpm_max"));
}

#[test]
fn a_missing_source_loses_only_its_own_readings() {
    let directory = tempfile::tempdir().unwrap();
    kernel_tree(directory.path(), directory.path());
    // No sensors and no default route on this sample.
    fs::remove_dir_all(directory.path().join("sys/class/hwmon")).unwrap();
    fs::remove_file(directory.path().join("proc/net/route")).unwrap();
    let read = read_host(&roots(directory.path()), directory.path());
    assert_eq!(read.board_temperature_max_c, None);
    assert_eq!(read.fan_rpm_max, None);
    assert_eq!(read.network_lan_rx_bytes, None);
    assert_eq!(read.cpu_busy_seconds, Some(852046.01));
    assert_eq!(read.fabric_ports_active, Some(2));
    assert_eq!(read.oom_kills, Some(124));
}
