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
/// `/proc/stat` counts in `USER_HZ`, which is 100 on every Linux ABI we run.
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
#[expect(
    clippy::cast_precision_loss,
    reason = "clock ticks converted to seconds; f64 precision is ample"
)]
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

/// One RDMA device's port 1: its state and counters (4-byte words, as bytes).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FabricDeviceReading {
    pub name: String,
    /// The PCI bus:slot.function behind the device, shared by the two devices
    /// of one cabled port (their PCI domains differ); `None` when unreadable.
    pub port: Option<String>,
    pub active: bool,
    pub rx_bytes: Option<u64>,
    pub tx_bytes: Option<u64>,
}

/// Every RDMA device under `root` (`/sys/class/infiniband`), by name; `None`
/// on a host without any.
pub fn read_fabric_devices(root: &Path) -> Option<Vec<FabricDeviceReading>> {
    let devices = sorted_entries(root)?;
    if devices.is_empty() {
        return None;
    }
    Some(
        devices
            .into_iter()
            .map(|device| {
                let port = device.join("ports/1");
                let words = |name: &str| {
                    read_bounded_text(&port.join("counters").join(name))?
                        .trim()
                        .parse::<u64>()
                        .ok()?
                        .checked_mul(4)
                };
                FabricDeviceReading {
                    name: device
                        .file_name()
                        .map_or_default(|name| name.to_string_lossy().into_owned()),
                    port: pci_function(&device),
                    active: read_bounded_text(&port.join("state"))
                        .is_some_and(|state| state.trim_start().starts_with("4:")),
                    rx_bytes: words("port_rcv_data"),
                    tx_bytes: words("port_xmit_data"),
                }
            })
            .collect(),
    )
}

/// RDMA `hw_counters` that count link and transport errors. `out_of_buffer`
/// and the netdev's `rx_dropped` are left out: they count receive pressure,
/// not a failing link.
const FABRIC_ERROR_COUNTERS: [&str; 6] = [
    "packet_seq_err",
    "local_ack_timeout_err",
    "rnr_nak_retry_err",
    "implied_nak_seq_err",
    "req_transport_retries_exceeded",
    "rx_icrc_encapsulated",
];

/// One RDMA device's negotiated rate in Mb/s (`ports/1/rate`, e.g.
/// `200 Gb/sec (2X NDR)`) and its cumulative link and transport errors (the
/// `hw_counters` above plus the netdev's `rx_errors` and `tx_errors`). Each is
/// `None` when any file it needs is unreadable. Read at the inventory cadence
/// only: `hw_counters` may query firmware.
pub fn read_fabric_health(root: &Path, name: &str) -> (Option<u32>, Option<u64>) {
    let device = root.join(name);
    let rate = read_bounded_text(&device.join("ports/1/rate")).and_then(|text| {
        // `200` or `2.5` Gb/sec; IB rates have at most one decimal.
        let token = text.split_ascii_whitespace().next()?;
        let (whole, tenths) = token.split_once('.').unwrap_or((token, "0"));
        let mbps = whole
            .parse::<u32>()
            .ok()?
            .checked_mul(1000)?
            .checked_add(tenths.parse::<u32>().ok().filter(|t| *t < 10)? * 100)?;
        (1..=1_000_000).contains(&mbps).then_some(mbps)
    });
    (rate, fabric_errors(&device))
}

fn fabric_errors(device: &Path) -> Option<u64> {
    let count = |path: PathBuf| {
        let value = read_bounded_text(&path)?.trim().parse::<u64>().ok()?;
        (value <= COUNTER_MAX).then_some(value)
    };
    let mut netdevs = sorted_entries(&device.join("device/net"))?.into_iter();
    let netdev = netdevs.next().filter(|_| netdevs.next().is_none())?;
    let mut counters = FABRIC_ERROR_COUNTERS
        .iter()
        .map(|name| device.join("ports/1/hw_counters").join(name))
        .chain(["rx_errors", "tx_errors"].map(|name| netdev.join("statistics").join(name)));
    counters.try_fold(0u64, |sum, path| sum.checked_add(count(path)?))
}

/// `bus:slot.function` of the PCI device a `device` symlink names
/// (`0002:01:00.1` gives `01:00.1`).
fn pci_function(device: &Path) -> Option<String> {
    let target = std::fs::canonicalize(device.join("device")).ok()?;
    let address = target.file_name()?.to_str()?;
    let (_, function) = address.split_once(':')?;
    Some(function.to_owned())
}

/// RDMA traffic of all ports (a port going down must not make the counters
/// drop) and the number of ACTIVE ports. The fabric's netdev counters miss it
/// (RDMA bypasses them); the port counters are in 4-byte words.
fn read_fabric(root: &Path) -> Option<Fabric> {
    let mut fabric = Fabric {
        rx_bytes: 0,
        tx_bytes: 0,
        ports_active: 0,
    };
    for device in read_fabric_devices(root)? {
        if let (Some(rx), Some(tx)) = (device.rx_bytes, device.tx_bytes) {
            fabric.rx_bytes = fabric.rx_bytes.checked_add(rx)?;
            fabric.tx_bytes = fabric.tx_bytes.checked_add(tx)?;
        }
        fabric.ports_active += u32::from(device.active);
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
/// is the board sensor), the `NVMe` composite, the NIC ASICs and the ACPI fans.
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
                    .max(found.fan_rpm);
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
mod tests;
