//! Evidence about the NICs and the interface this Spark reaches the NAS over.
//!
//! A Spark that reaches the NAS over Wi-Fi shares airtime for every model
//! transfer while its wired port may sit unplugged. The agent only reports
//! what the kernel says (interface kind, negotiated speed, carrier, and which
//! interface the NAS address routes through); the Controller owns the warning.

use std::{
    fs,
    net::{IpAddr, Ipv4Addr, ToSocketAddrs},
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

/// Resolve the Controller (NAS) host to an IPv4 address when it is one.
pub fn nas_address(controller_url: &Url) -> Option<Ipv4Addr> {
    match controller_url.host()? {
        Host::Ipv4(address) => Some(address),
        Host::Ipv6(_) => None,
        Host::Domain(name) => (name, controller_url.port_or_known_default()?)
            .to_socket_addrs()
            .ok()?
            .find_map(|address| match address.ip() {
                IpAddr::V4(address) => Some(address),
                IpAddr::V6(_) => None,
            }),
    }
}

pub fn collect(
    sys_class_net: &Path,
    proc_net_route: &Path,
    nas: Option<Ipv4Addr>,
) -> NetworkEvidence {
    let route = nas.and_then(|address| {
        fs::read_to_string(proc_net_route)
            .ok()
            .and_then(|table| route_interface(&table, address))
    });
    let Ok(entries) = fs::read_dir(sys_class_net) else {
        return NetworkEvidence::default();
    };
    let mut interfaces: Vec<NetworkInterface> = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name().into_string().ok()?;
            let physical = entry.path().join("device").exists();
            if !valid_name(&name) || (!physical && route.as_deref() != Some(name.as_str())) {
                return None;
            }
            Some(interface(&entry.path(), name))
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

fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= MAX_NAME_BYTES
        && name.starts_with(|value: char| value.is_ascii_alphanumeric())
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn interface(path: &Path, name: String) -> NetworkInterface {
    let read = |file: &str| fs::read_to_string(path.join(file)).ok();
    let wireless = path.join("wireless").exists() || path.join("phy80211").exists();
    let ethernet = read("type").is_some_and(|value| value.trim() == "1");
    let kind = if wireless {
        NetworkInterfaceKind::Wifi
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
        let evidence = collect(directory.path(), &table, Some("10.0.0.5".parse().unwrap()));
        let interfaces = evidence.interfaces.unwrap();
        assert_eq!(interfaces[0].link_speed_mbps, Some(10_000));
        assert_eq!(evidence.nas_route_interface.as_deref(), Some("enP7s7"));
    }

    #[test]
    fn unreadable_sysfs_is_unknown_not_empty() {
        let evidence = collect(
            Path::new("/nonexistent-sysfs"),
            Path::new("/nonexistent"),
            None,
        );
        assert_eq!(evidence, NetworkEvidence::default());
    }
}
