//! Host readings that need no privilege: `/proc`, `/sys` files read as the
//! login-class monitor user. Each reading fails to `None` on its own and the
//! next sample reads again; nothing is latched or remembered.

use std::path::{Path, PathBuf};

use super::read_bounded_text;

/// Roots of the kernel files, so a test can point them at a copy.
#[derive(Debug, Clone)]
pub struct HostRoots {
    pub proc: PathBuf,
    pub sys: PathBuf,
}

impl Default for HostRoots {
    fn default() -> Self {
        Self {
            proc: PathBuf::from("/proc"),
            sys: PathBuf::from("/sys"),
        }
    }
}

#[derive(Debug, Default, PartialEq)]
pub struct HostReadings {
    pub cpu_busy_seconds: Option<f64>,
    pub cpu_total_seconds: Option<f64>,
    pub load_average_1m: Option<f64>,
    pub network_lan_rx_bytes: Option<u64>,
    pub network_lan_tx_bytes: Option<u64>,
    pub fabric_rx_bytes: Option<u64>,
    pub fabric_tx_bytes: Option<u64>,
    pub fabric_ports_active: Option<u32>,
    pub disk_read_bytes: Option<u64>,
    pub disk_written_bytes: Option<u64>,
    pub oom_kills: Option<u64>,
    pub board_temperature_max_c: Option<u32>,
    pub nvme_temperature_c: Option<u32>,
    pub nic_temperature_c: Option<u32>,
    pub fan_rpm_max: Option<u32>,
}

const COUNTER_MAX: u64 = i64::MAX as u64;
const MAX_TEMPERATURE_C: u32 = 150;
const MAX_FAN_RPM: u32 = 100_000;
const MAX_FABRIC_PORTS: u32 = 16;
const MAX_DIRECTORY_ENTRIES: usize = 256;
/// `/proc/stat` counts in USER_HZ, which is 100 on every Linux ABI we run.
const CLOCK_TICKS_PER_SECOND: f64 = 100.0;

pub fn read_host(roots: &HostRoots, store: &Path) -> HostReadings {
    let cpu = read_bounded_text(&roots.proc.join("stat")).and_then(|text| parse_cpu(&text));
    let lan = read_lan(&roots.proc);
    let fabric = read_fabric(&roots.sys.join("class/infiniband"));
    let disk = read_disk(&roots.sys, store);
    let hwmon = read_hwmon(&roots.sys.join("class/hwmon"));
    HostReadings {
        cpu_busy_seconds: cpu.map(|value| value.0),
        cpu_total_seconds: cpu.map(|value| value.1),
        load_average_1m: read_bounded_text(&roots.proc.join("loadavg"))
            .and_then(|text| text.split_ascii_whitespace().next()?.parse::<f64>().ok())
            .filter(|value| value.is_finite() && *value >= 0.0),
        network_lan_rx_bytes: lan.map(|value| value.0),
        network_lan_tx_bytes: lan.map(|value| value.1),
        fabric_rx_bytes: fabric.map(|value| value.rx_bytes),
        fabric_tx_bytes: fabric.map(|value| value.tx_bytes),
        fabric_ports_active: fabric.map(|value| value.ports_active),
        disk_read_bytes: disk.map(|value| value.0),
        disk_written_bytes: disk.map(|value| value.1),
        oom_kills: read_bounded_text(&roots.proc.join("vmstat")).and_then(|text| parse_oom(&text)),
        board_temperature_max_c: hwmon.board,
        nvme_temperature_c: hwmon.nvme,
        nic_temperature_c: hwmon.nic,
        fan_rpm_max: hwmon.fan_rpm,
    }
}

/// (busy, total) seconds from the aggregate `cpu` line: user nice system idle
/// iowait irq softirq steal. Busy is all of it except idle and I/O wait.
fn parse_cpu(text: &str) -> Option<(f64, f64)> {
    let line = text.lines().find(|line| line.starts_with("cpu "))?;
    let ticks = line
        .split_ascii_whitespace()
        .skip(1)
        .take(8)
        .map(|field| field.parse::<u64>().ok())
        .collect::<Option<Vec<_>>>()?;
    let (idle, iowait) = (*ticks.get(3)?, *ticks.get(4)?);
    let total = ticks.iter().sum::<u64>();
    let busy = total.checked_sub(idle + iowait)?;
    Some((
        busy as f64 / CLOCK_TICKS_PER_SECOND,
        total as f64 / CLOCK_TICKS_PER_SECOND,
    ))
}

fn parse_oom(text: &str) -> Option<u64> {
    text.lines()
        .find_map(|line| line.strip_prefix("oom_kill "))?
        .trim()
        .parse::<u64>()
        .ok()
        .filter(|value| *value <= COUNTER_MAX)
}

/// Counters of the interface that carries the default route.
fn read_lan(proc: &Path) -> Option<(u64, u64)> {
    let interface = default_route_interface(&read_bounded_text(&proc.join("net/route"))?)?;
    interface_bytes(&read_bounded_text(&proc.join("net/dev"))?, &interface)
}

/// The interface of the default route (destination `00000000`) with the lowest metric.
fn default_route_interface(route: &str) -> Option<String> {
    // Iface Destination Gateway Flags RefCnt Use Metric Mask
    let defaults = route.lines().skip(1).filter_map(|line| {
        let fields = line.split_ascii_whitespace().collect::<Vec<_>>();
        if fields.get(1) != Some(&"00000000") || fields.get(7) != Some(&"00000000") {
            return None;
        }
        Some((
            fields.get(6)?.parse::<u64>().ok()?,
            (*fields.first()?).to_owned(),
        ))
    });
    defaults.min().map(|(_, interface)| interface)
}

fn interface_bytes(net_dev: &str, interface: &str) -> Option<(u64, u64)> {
    let line = net_dev.lines().find_map(|line| {
        let (name, counters) = line.split_once(':')?;
        (name.trim() == interface).then_some(counters)
    })?;
    let fields = line.split_ascii_whitespace().collect::<Vec<_>>();
    let rx = fields.first()?.parse::<u64>().ok()?;
    let tx = fields.get(8)?.parse::<u64>().ok()?;
    (rx <= COUNTER_MAX && tx <= COUNTER_MAX).then_some((rx, tx))
}

#[derive(Debug, Clone, Copy)]
struct Fabric {
    rx_bytes: u64,
    tx_bytes: u64,
    ports_active: u32,
}

/// RDMA traffic of the ACTIVE ports. The fabric's netdev counters miss it
/// (RDMA bypasses them); the port counters are in 4-byte words.
fn read_fabric(root: &Path) -> Option<Fabric> {
    let mut fabric = Fabric {
        rx_bytes: 0,
        tx_bytes: 0,
        ports_active: 0,
    };
    // A host without RDMA devices has no fabric to report.
    let devices = sorted_entries(root)?;
    if devices.is_empty() {
        return None;
    }
    for device in devices {
        let port = device.join("ports/1");
        let active = read_bounded_text(&port.join("state"))
            .is_some_and(|state| state.trim_start().starts_with("4:"));
        let words = |name: &str| {
            read_bounded_text(&port.join("counters").join(name))?
                .trim()
                .parse::<u64>()
                .ok()?
                .checked_mul(4)
        };
        if let (true, Some(rx), Some(tx)) =
            (active, words("port_rcv_data"), words("port_xmit_data"))
        {
            fabric.rx_bytes = fabric.rx_bytes.checked_add(rx)?;
            fabric.tx_bytes = fabric.tx_bytes.checked_add(tx)?;
            fabric.ports_active += 1;
        }
    }
    (fabric.rx_bytes <= COUNTER_MAX
        && fabric.tx_bytes <= COUNTER_MAX
        && fabric.ports_active <= MAX_FABRIC_PORTS)
        .then_some(fabric)
}

/// (read, written) bytes of the whole disk behind the model store.
fn read_disk(sys: &Path, store: &Path) -> Option<(u64, u64)> {
    let device = rustix::fs::stat(store).ok()?.st_dev;
    let link = sys.join(format!(
        "dev/block/{}:{}",
        rustix::fs::major(device),
        rustix::fs::minor(device)
    ));
    let mut block = std::fs::canonicalize(link).ok()?;
    if block.join("partition").exists() {
        block = block.parent()?.to_path_buf();
    }
    parse_block_stat(&read_bounded_text(&block.join("stat"))?)
}

/// Sectors read and written (fields 3 and 7), 512 bytes each.
fn parse_block_stat(text: &str) -> Option<(u64, u64)> {
    let fields = text.split_ascii_whitespace().collect::<Vec<_>>();
    let read = fields.get(2)?.parse::<u64>().ok()?.checked_mul(512)?;
    let written = fields.get(6)?.parse::<u64>().ok()?.checked_mul(512)?;
    (read <= COUNTER_MAX && written <= COUNTER_MAX).then_some((read, written))
}

#[derive(Debug, Default)]
struct Hwmon {
    board: Option<u32>,
    nvme: Option<u32>,
    nic: Option<u32>,
    fan_rpm: Option<u32>,
}

/// Sensors are chosen by the chip `name`: the ACPI thermal zones (the hottest
/// is the board sensor), the NVMe composite, the NIC ASICs and the ACPI fans.
fn read_hwmon(root: &Path) -> Hwmon {
    let mut found = Hwmon::default();
    for chip in sorted_entries(root).unwrap_or_default() {
        let Some(name) = read_bounded_text(&chip.join("name")) else {
            continue;
        };
        let number = |file: &str| {
            read_bounded_text(&chip.join(file))?
                .trim()
                .parse::<i64>()
                .ok()
        };
        match name.trim() {
            "acpitz" => found.board = max_temperature(&chip, found.board, |_| true),
            "mlx5" => found.nic = max_temperature(&chip, found.nic, |_| true),
            "nvme" => found.nvme = max_temperature(&chip, found.nvme, |label| label == "Composite"),
            "acpi_fan" => {
                found.fan_rpm = number("fan1_input")
                    .and_then(|rpm| u32::try_from(rpm).ok())
                    .filter(|rpm| *rpm <= MAX_FAN_RPM)
                    .max(found.fan_rpm)
            }
            _ => {}
        }
    }
    found
}

/// Highest `temp*_input` of a chip whose label (when it has one) is accepted,
/// in whole °C, floored. Values outside 0..=150 are not readings.
fn max_temperature(chip: &Path, current: Option<u32>, label: impl Fn(&str) -> bool) -> Option<u32> {
    let mut best = current;
    for index in 1..=16 {
        let Some(milli) = read_bounded_text(&chip.join(format!("temp{index}_input")))
            .and_then(|text| text.trim().parse::<i64>().ok())
        else {
            continue;
        };
        let labelled = read_bounded_text(&chip.join(format!("temp{index}_label")));
        if labelled.as_deref().is_some_and(|text| !label(text.trim())) {
            continue;
        }
        if let Ok(celsius) = u32::try_from(milli.div_euclid(1000))
            && celsius <= MAX_TEMPERATURE_C
        {
            best = best.max(Some(celsius));
        }
    }
    best
}

fn sorted_entries(root: &Path) -> Option<Vec<PathBuf>> {
    let mut entries = std::fs::read_dir(root)
        .ok()?
        .flatten()
        .map(|entry| entry.path())
        .take(MAX_DIRECTORY_ENTRIES)
        .collect::<Vec<_>>();
    entries.sort();
    Some(entries)
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::*;

    fn vector() -> serde_json::Value {
        serde_json::from_str(include_str!(
            "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/telemetry-sample-v2.json"
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
}
