//! Evidence about the NICs and the interface this Spark reaches the NAS over.
//!
//! A Spark that reaches the NAS over Wi-Fi shares airtime for every model
//! transfer while its wired port may sit unplugged. The agent only reports
//! what the kernel says (interface kind, negotiated speed, carrier, and which
//! interface the NAS address routes through, over IPv4 or IPv6); the Controller
//! owns the warning. A route through a virtual overlay (Tailscale, WireGuard) is
//! reported as a `tunnel` interface, which is never a Wi-Fi finding.

use std::{
    collections::BTreeSet,
    fs,
    net::{IpAddr, Ipv4Addr, Ipv6Addr, ToSocketAddrs},
    path::Path,
};

use url::{Host, Url};
use vonk_agent_protocol::generated::{NetworkInterface, NetworkInterfaceKind};

const MAX_INTERFACES: usize = 16;
const MAX_NAME_BYTES: usize = 15;

/// `interfaces` is `None` when the evidence could not be gathered at all, so
/// the Controller treats it as unknown rather than as "no NICs".
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct NetworkEvidence {
    pub interfaces: Option<Vec<NetworkInterface>>,
    pub nas_route_interface: Option<String>,
}

/// Resolve the Controller (NAS) host to the address the agent will connect to:
/// an IPv4 address when the host has one, otherwise its IPv6 address.
pub fn nas_address(controller_url: &Url) -> Option<IpAddr> {
    match controller_url.host()? {
        Host::Ipv4(address) => Some(IpAddr::V4(address)),
        Host::Ipv6(address) => Some(IpAddr::V6(address)),
        Host::Domain(name) => {
            let addresses: Vec<IpAddr> = (name, controller_url.port_or_known_default()?)
                .to_socket_addrs()
                .ok()?
                .map(|address| address.ip())
                .collect();
            addresses
                .iter()
                .copied()
                .find(IpAddr::is_ipv4)
                .or_else(|| addresses.first().copied())
        }
    }
}

pub fn collect(
    sys_class_net: &Path,
    proc_net_route: &Path,
    proc_net_ipv6_route: &Path,
    nas: Option<IpAddr>,
) -> NetworkEvidence {
    let route = nas.and_then(|address| match address {
        IpAddr::V4(address) => read_route(proc_net_route, |table| route_interface(table, address)),
        IpAddr::V6(address) => match address.to_ipv4_mapped() {
            Some(mapped) => read_route(proc_net_route, |table| route_interface(table, mapped)),
            None => read_route(proc_net_ipv6_route, |table| {
                route_interface_v6(table, address)
            }),
        },
    });
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

fn read_route(path: &Path, find: impl Fn(&str) -> Option<String>) -> Option<String> {
    fs::read_to_string(path).ok().and_then(|table| find(&table))
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

/// Longest-prefix IPv4 route for `destination` from a `/proc/net/route` body.
fn route_interface(table: &str, destination: Ipv4Addr) -> Option<String> {
    let target = u32::from(destination);
    table
        .lines()
        .skip(1)
        .filter_map(|line| {
            let fields: Vec<&str> = line.split_whitespace().collect();
            let [name, network, _, flags, _, _, metric, mask] = fields.get(..8)? else {
                return None;
            };
            // The kernel prints these as host-order hex of a little-endian word.
            let network = u32::from_str_radix(network, 16).ok()?.swap_bytes();
            let mask = u32::from_str_radix(mask, 16).ok()?.swap_bytes();
            let flags = u32::from_str_radix(flags, 16).ok()?;
            let metric = metric.parse::<u32>().ok()?;
            (flags & 1 == 1 && target & mask == network).then(|| {
                (
                    mask.count_ones(),
                    std::cmp::Reverse(metric),
                    (*name).to_owned(),
                )
            })
        })
        .max()
        .map(|(_, _, name)| name)
}

/// Longest-prefix IPv6 route for `destination` from a `/proc/net/ipv6_route` body.
///
/// Each line is `dest(32 hex) prefix(2 hex) src(32) prefix(2) nexthop(32)
/// metric refcnt use flags(8 hex) iface`; a rejected or down route is skipped.
fn route_interface_v6(table: &str, destination: Ipv6Addr) -> Option<String> {
    const RTF_UP: u32 = 0x1;
    const RTF_REJECT: u32 = 0x200;
    let target = u128::from(destination);
    table
        .lines()
        .filter_map(|line| {
            let fields: Vec<&str> = line.split_whitespace().collect();
            let [network, prefix, _, _, _, metric, _, _, flags, name] = fields.get(..10)? else {
                return None;
            };
            let network = u128::from_str_radix(network, 16).ok()?;
            let prefix = u32::from_str_radix(prefix, 16)
                .ok()
                .filter(|value| *value <= 128)?;
            let metric = u32::from_str_radix(metric, 16).ok()?;
            let flags = u32::from_str_radix(flags, 16).ok()?;
            let mask = if prefix == 0 {
                0
            } else {
                u128::MAX << (128 - prefix)
            };
            (flags & RTF_UP != 0 && flags & RTF_REJECT == 0 && target & mask == network & mask)
                .then(|| (prefix, std::cmp::Reverse(metric), (*name).to_owned()))
        })
        .max()
        .map(|(_, _, name)| name)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    // 192.168.1.0/24 via wlP9s9 and a default route via the wired port.
    const TABLE: &str = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n\
wlP9s9\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n\
enP7s7\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n";

    fn nic(root: &Path, name: &str, kind: &str, carrier: Option<&str>, speed: Option<&str>) {
        let path = root.join(name);
        fs::create_dir_all(path.join("device")).unwrap();
        fs::write(path.join("type"), "1\n").unwrap();
        if kind == "wifi" {
            fs::create_dir(path.join("wireless")).unwrap();
        }
        if let Some(value) = carrier {
            fs::write(path.join("carrier"), value).unwrap();
        }
        if let Some(value) = speed {
            fs::write(path.join("speed"), value).unwrap();
        }
    }

    #[test]
    fn the_longest_prefix_wins_over_the_default_route() {
        let nas = "192.168.1.20".parse().unwrap();
        assert_eq!(route_interface(TABLE, nas).as_deref(), Some("wlP9s9"));
        let elsewhere = "10.0.0.5".parse().unwrap();
        assert_eq!(route_interface(TABLE, elsewhere).as_deref(), Some("enP7s7"));
    }

    #[test]
    fn reports_a_wifi_route_and_a_wired_port_without_link() {
        let directory = tempdir().unwrap();
        nic(directory.path(), "wlP9s9", "wifi", Some("1\n"), None);
        // A NIC without link: carrier and speed read as unavailable or -1.
        nic(
            directory.path(),
            "enP7s7",
            "wired",
            Some("0\n"),
            Some("-1\n"),
        );
        fs::create_dir(directory.path().join("lo")).unwrap();
        let table = directory.path().join("route");
        fs::write(&table, TABLE).unwrap();
        let evidence = collect(
            directory.path(),
            &table,
            Path::new("/nonexistent-ipv6"),
            Some("192.168.1.20".parse().unwrap()),
        );

        assert_eq!(evidence.nas_route_interface.as_deref(), Some("wlP9s9"));
        let interfaces = evidence.interfaces.unwrap();
        assert_eq!(
            interfaces
                .iter()
                .map(|value| (value.name.as_str(), value.kind, value.carrier))
                .collect::<Vec<_>>(),
            vec![
                ("enP7s7", NetworkInterfaceKind::Wired, false),
                ("wlP9s9", NetworkInterfaceKind::Wifi, true),
            ]
        );
        assert_eq!(interfaces[0].link_speed_mbps, None);
    }

    #[test]
    fn a_linked_wired_port_reports_its_negotiated_speed() {
        let directory = tempdir().unwrap();
        nic(
            directory.path(),
            "enP7s7",
            "wired",
            Some("1\n"),
            Some("10000\n"),
        );
        let table = directory.path().join("route");
        fs::write(&table, TABLE).unwrap();
        let evidence = collect(
            directory.path(),
            &table,
            Path::new("/nonexistent-ipv6"),
            Some("10.0.0.5".parse().unwrap()),
        );
        let interfaces = evidence.interfaces.unwrap();
        assert_eq!(interfaces[0].link_speed_mbps, Some(10_000));
        assert_eq!(evidence.nas_route_interface.as_deref(), Some("enP7s7"));
    }

    #[test]
    fn rdma_and_connectx_ports_are_fabric_not_wired() {
        let directory = tempdir().unwrap();
        let net = directory.path().join("net");
        nic(&net, "enP7s7", "wired", Some("0\n"), None);
        nic(
            &net,
            "enP2p1s0f1np1",
            "wired",
            Some("1\n"),
            Some("200000\n"),
        );
        nic(&net, "enp1s0f1np1", "wired", Some("1\n"), Some("200000\n"));
        // RDMA membership names the first port; the driver names the second.
        fs::create_dir_all(
            directory
                .path()
                .join("infiniband/rocep1s0f1/device/net/enP2p1s0f1np1"),
        )
        .unwrap();
        let driver = directory.path().join("mlx5_core");
        fs::create_dir(&driver).unwrap();
        std::os::unix::fs::symlink(&driver, net.join("enp1s0f1np1/device/driver")).unwrap();
        let table = directory.path().join("route");
        fs::write(&table, TABLE).unwrap();
        let evidence = collect(
            &net,
            &table,
            Path::new("/nonexistent-ipv6"),
            Some("10.0.0.5".parse().unwrap()),
        );
        let kinds = evidence
            .interfaces
            .unwrap()
            .iter()
            .map(|value| (value.name.clone(), value.kind))
            .collect::<Vec<_>>();
        assert_eq!(
            kinds,
            vec![
                ("enP2p1s0f1np1".to_owned(), NetworkInterfaceKind::Fabric),
                ("enP7s7".to_owned(), NetworkInterfaceKind::Wired),
                ("enp1s0f1np1".to_owned(), NetworkInterfaceKind::Fabric),
            ]
        );
    }

    #[test]
    fn unreadable_sysfs_is_unknown_not_empty() {
        let evidence = collect(
            Path::new("/nonexistent-sysfs"),
            Path::new("/nonexistent"),
            Path::new("/nonexistent-ipv6"),
            None,
        );
        assert_eq!(evidence, NetworkEvidence::default());
    }

    // fd7a:115c:a1e0::/48 is Tailscale's ULA range, routed through tailscale0;
    // the default route leaves through the wired port.
    const TABLE_V6: &str = "\
fd7a115ca1e000000000000000000000 30 00000000000000000000000000000000 00 00000000000000000000000000000000 00000400 00000001 00000000 00000001 tailscale0\n\
00000000000000000000000000000000 00 00000000000000000000000000000000 00 fe800000000000000000000000000001 00000400 00000001 00000000 00000003 enP7s7\n\
00000000000000000000000000000000 80 00000000000000000000000000000000 00 00000000000000000000000000000000 ffffffff 00000001 00000000 00200001 lo\n";

    #[test]
    fn an_ipv6_route_is_the_longest_prefix_that_is_up() {
        let tailscale: Ipv6Addr = "fd7a:115c:a1e0::1".parse().unwrap();
        let outside: Ipv6Addr = "2001:db8::1".parse().unwrap();
        let table = TABLE_V6;
        assert_eq!(
            route_interface_v6(table, tailscale).as_deref(),
            Some("tailscale0")
        );
        assert_eq!(
            route_interface_v6(table, outside).as_deref(),
            Some("enP7s7")
        );
    }

    #[test]
    fn a_tailscale_route_is_reported_as_a_tunnel_without_hardware() {
        let directory = tempdir().unwrap();
        nic(
            directory.path(),
            "enP7s7",
            "wired",
            Some("1\n"),
            Some("1000\n"),
        );
        // No device directory: a virtual TUN interface, as the kernel names it.
        let tunnel = directory.path().join("tailscale0");
        fs::create_dir_all(&tunnel).unwrap();
        fs::write(tunnel.join("type"), "65534\n").unwrap();
        fs::write(tunnel.join("operstate"), "unknown\n").unwrap();
        let v4 = directory.path().join("route");
        fs::write(
            &v4,
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n\
tailscale0\t00000064\t00000000\t0001\t0\t0\t0\t000000FF\t0\t0\t0\n",
        )
        .unwrap();
        let evidence = collect(
            directory.path(),
            &v4,
            Path::new("/nonexistent-ipv6"),
            Some("100.1.2.3".parse().unwrap()),
        );
        assert_eq!(evidence.nas_route_interface.as_deref(), Some("tailscale0"));
        let kinds: Vec<_> = evidence
            .interfaces
            .unwrap()
            .into_iter()
            .map(|value| (value.name, value.kind))
            .collect();
        assert!(kinds.contains(&("tailscale0".to_owned(), NetworkInterfaceKind::Tunnel)));
        assert!(kinds.contains(&("enP7s7".to_owned(), NetworkInterfaceKind::Wired)));
    }

    #[test]
    fn an_ipv6_nas_address_is_routed_through_the_ipv6_table() {
        let directory = tempdir().unwrap();
        nic(
            directory.path(),
            "enP7s7",
            "wired",
            Some("1\n"),
            Some("1000\n"),
        );
        let tunnel = directory.path().join("tailscale0");
        fs::create_dir_all(&tunnel).unwrap();
        fs::write(tunnel.join("type"), "65534\n").unwrap();
        let v6 = directory.path().join("ipv6_route");
        fs::write(&v6, TABLE_V6).unwrap();
        let evidence = collect(
            directory.path(),
            Path::new("/nonexistent"),
            &v6,
            Some("fd7a:115c:a1e0::5".parse().unwrap()),
        );
        assert_eq!(evidence.nas_route_interface.as_deref(), Some("tailscale0"));
    }

    #[test]
    fn nas_address_keeps_an_ipv6_literal_and_prefers_ipv4_for_a_name() {
        let literal = Url::parse("https://[fd7a:115c:a1e0::5]:8443").unwrap();
        assert_eq!(
            nas_address(&literal),
            Some("fd7a:115c:a1e0::5".parse().unwrap())
        );
        let v4 = Url::parse("https://192.168.1.231:8443").unwrap();
        assert_eq!(nas_address(&v4), Some("192.168.1.231".parse().unwrap()));
    }
}
