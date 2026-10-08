//! Evidence about the NICs and the interface this Spark reaches the NAS over.
//!
//! A Spark that reaches the NAS over Wi-Fi shares airtime for every model
//! transfer while its wired port may sit unplugged. The agent only reports
//! what the kernel says (interface kind, negotiated speed, carrier, and which
//! interface the NAS address routes through, over IPv4 or IPv6); the Controller
//! owns the warning. A route through a virtual overlay (Tailscale, WireGuard) is
//! reported as a `tunnel` interface, which is never a Wi-Fi finding.

use std::{collections::BTreeSet, fs, net::IpAddr, path::Path, time::Duration};

use crate::process::{ProcessRunner, Program, SystemProcessRunner};
use url::{Host, Url};
use vonk_agent_protocol::generated::{
    AgentEvidenceCode, InventoryRequest, NetworkInterface, NetworkInterfaceKind,
};

const MAX_INTERFACES: usize = 16;
const MAX_NAME_BYTES: usize = 15;

/// `interfaces` is `None` when the evidence could not be gathered at all, so
/// the Controller treats it as unknown rather than as "no NICs".
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct NetworkEvidence {
    pub interfaces: Option<Vec<NetworkInterface>>,
    pub nas_route_interface: Option<String>,
}

/// Resolve every NAS address; mixed routes are unknown rather than a guess
/// about which address a pooled/Happy-Eyeballs HTTP connection actually uses.
fn nas_addresses(controller_url: &Url, runner: &impl ProcessRunner) -> Vec<IpAddr> {
    let Some(host) = controller_url.host() else {
        return vec![];
    };
    match host {
        Host::Ipv4(address) => vec![IpAddr::V4(address)],
        Host::Ipv6(address) => vec![IpAddr::V6(address)],
        Host::Domain(name) => {
            let Ok(output) = runner.run(
                Program::Getent,
                &["ahosts".into(), name.into()],
                Duration::from_millis(500),
            ) else {
                return vec![];
            };
            if !output.success {
                return vec![];
            }
            let Ok(text) = String::from_utf8(output.stdout) else {
                return vec![];
            };
            text.lines()
                .filter_map(|line| line.split_whitespace().next()?.parse().ok())
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect()
        }
    }
}

fn nas_route(controller_url: &Url, runner: &impl ProcessRunner) -> Option<String> {
    let deadline = std::time::Instant::now() + Duration::from_secs(1);
    let mut route = None;
    for address in nas_addresses(controller_url, runner) {
        if std::time::Instant::now() >= deadline {
            return None;
        }
        let selected = selected_route(runner, address)?;
        if route.as_ref().is_some_and(|previous| previous != &selected) {
            return None;
        }
        route = Some(selected);
    }
    route
}

fn collect_interfaces(sys_class_net: &Path, route: Option<String>) -> NetworkEvidence {
    let Ok(entries) = fs::read_dir(sys_class_net) else {
        return NetworkEvidence::default();
    };
    let rdma = rdma_interfaces(&sys_class_net.with_file_name("infiniband"));
    let mut interfaces: Vec<NetworkInterface> = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name().into_string().ok()?;
            let physical = entry.path().join("device").exists();
            if !valid_name(&name) || (!physical && route.as_deref() != Some(name.as_str())) {
                return None;
            }
            let fabric = rdma.contains(&name);
            Some(interface(&entry.path(), name, fabric))
        })
        .collect();
    // Keep the route interface when the list must be bounded.
    interfaces.sort_by(|left, right| {
        (route.as_deref() != Some(left.name.as_str()), &left.name)
            .cmp(&(route.as_deref() != Some(right.name.as_str()), &right.name))
    });
    interfaces.truncate(MAX_INTERFACES);
    interfaces.sort_by(|left, right| left.name.cmp(&right.name));
    let nas_route_interface =
        route.filter(|name| interfaces.iter().any(|value| &value.name == name));
    NetworkEvidence {
        interfaces: Some(interfaces),
        nas_route_interface,
    }
}

/// One bounded best-effort probe per process. A slow sysfs read cannot pile up
/// blocked workers across inventory polls: its permit lives until it finishes.
/// Mandatory inventory always continues, and the next poll retries once free.
pub async fn collect_optional(controller_url: Url) -> NetworkEvidence {
    static SLOT: tokio::sync::Semaphore = tokio::sync::Semaphore::const_new(1);
    bounded_probe_with_slot(
        move || {
            let runner = SystemProcessRunner;
            let route = nas_route(&controller_url, &runner);
            collect_interfaces(Path::new("/sys/class/net"), route)
        },
        Duration::from_secs(2),
        &SLOT,
    )
    .await
}

async fn bounded_probe_with_slot(
    probe: impl FnOnce() -> NetworkEvidence + Send + 'static,
    budget: Duration,
    slot: &'static tokio::sync::Semaphore,
) -> NetworkEvidence {
    let Ok(permit) = slot.try_acquire() else {
        return NetworkEvidence::default();
    };
    bounded_probe(
        move || {
            let _permit = permit;
            probe()
        },
        budget,
    )
    .await
}

async fn bounded_probe(
    probe: impl FnOnce() -> NetworkEvidence + Send + 'static,
    budget: Duration,
) -> NetworkEvidence {
    tokio::time::timeout(budget, tokio::task::spawn_blocking(probe))
        .await
        .ok()
        .and_then(Result::ok)
        .unwrap_or_default()
}

/// `ip route get` asks the kernel, including policy rules and Tailscale's
/// separate routing table. Main-table/default-route guesses are unsafe here.
fn selected_route(runner: &impl ProcessRunner, address: IpAddr) -> Option<String> {
    let output = runner
        .run(
            Program::Ip,
            &[
                "-o".into(),
                "route".into(),
                "get".into(),
                address.to_string(),
            ],
            Duration::from_millis(500),
        )
        .ok()?;
    if !output.success {
        return None;
    }
    let text = String::from_utf8(output.stdout).ok()?;
    let fields: Vec<_> = text.split_whitespace().collect();
    fields
        .windows(2)
        .find_map(|pair| (pair[0] == "dev" && valid_name(pair[1])).then(|| pair[1].to_owned()))
}

fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= MAX_NAME_BYTES
        && name.starts_with(|value: char| value.is_ascii_alphanumeric())
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

/// Net devices that back an RDMA device (`infiniband/*/device/net/*`).
fn rdma_interfaces(infiniband: &Path) -> BTreeSet<String> {
    let Ok(devices) = fs::read_dir(infiniband) else {
        return BTreeSet::new();
    };
    devices
        .filter_map(Result::ok)
        .filter_map(|device| fs::read_dir(device.path().join("device/net")).ok())
        .flat_map(|nets| nets.filter_map(Result::ok))
        .filter_map(|net| net.file_name().into_string().ok())
        .collect()
}

fn mellanox_driver(path: &Path) -> bool {
    fs::read_link(path.join("device/driver"))
        .ok()
        .and_then(|target| {
            target
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
        })
        .is_some_and(|name| name.starts_with("mlx5") || name.starts_with("mlx4"))
}

/// A virtual point-to-point overlay (Tailscale, WireGuard, a TUN device): no
/// backing hardware, and the kernel reports it as ARPHRD_NONE (65534).
fn virtual_overlay(path: &Path, name: &str) -> bool {
    !path.join("device").exists()
        && (fs::read_to_string(path.join("type")).is_ok_and(|value| value.trim() == "65534")
            || ["tailscale", "wg", "tun"]
                .iter()
                .any(|prefix| name.starts_with(prefix)))
}

fn interface(path: &Path, name: String, rdma: bool) -> NetworkInterface {
    let read = |file: &str| fs::read_to_string(path.join(file)).ok();
    let wireless = path.join("wireless").exists() || path.join("phy80211").exists();
    let ethernet = read("type").is_some_and(|value| value.trim() == "1");
    let kind = if wireless {
        NetworkInterfaceKind::Wifi
    } else if virtual_overlay(path, &name) {
        NetworkInterfaceKind::Tunnel
    } else if rdma || mellanox_driver(path) {
        NetworkInterfaceKind::Fabric
    } else if ethernet && path.join("device").exists() {
        NetworkInterfaceKind::Wired
    } else {
        NetworkInterfaceKind::Other
    };
    // `carrier` and `speed` are unreadable (EINVAL) while the link is down.
    let carrier = match read("carrier") {
        Some(value) => value.trim() == "1",
        None => read("operstate").is_some_and(|value| value.trim() == "up"),
    };
    let link_speed_mbps = read("speed")
        .and_then(|value| value.trim().parse::<i64>().ok())
        .filter(|value| (1..=1_000_000).contains(value))
        .and_then(|value| u32::try_from(value).ok());
    NetworkInterface {
        name,
        kind,
        link_speed_mbps: if carrier { link_speed_mbps } else { None },
        carrier,
    }
}

/// Keep the consistent part of the NIC evidence; invalid optional evidence never refuses inventory.
pub(crate) fn clamp_network_evidence(
    request: &mut InventoryRequest,
    warnings: &mut Vec<AgentEvidenceCode>,
) {
    const MAX_INTERFACES: usize = 16;
    if let Some(interfaces) = request.network_interfaces.as_mut() {
        let reported = interfaces.len();
        let mut seen = std::collections::BTreeSet::new();
        interfaces.retain_mut(|interface| {
            if !vonk_agent_protocol::valid_interface_name(&interface.name)
                || !seen.insert(interface.name.clone())
            {
                return false;
            }
            if interface
                .link_speed_mbps
                .is_some_and(|speed| !(1..=1_000_000).contains(&speed))
            {
                interface.link_speed_mbps = None;
            }
            true
        });
        if interfaces.len() > MAX_INTERFACES {
            // Keep the interface the NAS route uses when the list must be bounded.
            let route = request.nas_route_interface.clone();
            interfaces.sort_by_key(|interface| Some(&interface.name) != route.as_ref());
            interfaces.truncate(MAX_INTERFACES);
        }
        if interfaces.len() != reported {
            warnings.push(AgentEvidenceCode::AgentEvidenceInventoryNetworkInterfaceDropped);
        }
        if reported > 0 && interfaces.is_empty() {
            // Every reported NIC was invalid: this is unknown evidence, not
            // evidence that the host has no NICs.
            request.network_interfaces = None;
            warnings.push(AgentEvidenceCode::AgentEvidenceInventoryNetworkDropped);
        }
    }
    let route_reported = request.nas_route_interface.as_deref().is_none_or(|route| {
        request
            .network_interfaces
            .as_deref()
            .is_some_and(|interfaces| interfaces.iter().any(|value| value.name == route))
    });
    if !route_reported {
        request.nas_route_interface = None;
        warnings.push(AgentEvidenceCode::AgentEvidenceInventoryNasRouteDropped);
    }
}

#[cfg(test)]
#[path = "network_tests.rs"]
mod tests;
