#![cfg(test)]

use super::*;
use tempfile::tempdir;

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
    let evidence = collect_interfaces(directory.path(), Some("wlP9s9".into()));

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
    let evidence = collect_interfaces(directory.path(), Some("enP7s7".into()));
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
    let evidence = collect_interfaces(&net, Some("enP7s7".into()));
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
    let evidence = collect_interfaces(Path::new("/nonexistent-sysfs"), None);
    assert_eq!(evidence, NetworkEvidence::default());
    let recovered = tempdir().unwrap();
    nic(recovered.path(), "enP7s7", "wired", Some("1\n"), None);
    assert!(
        collect_interfaces(recovered.path(), Some("enP7s7".into()))
            .interfaces
            .is_some()
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
    let evidence = collect_interfaces(directory.path(), Some("tailscale0".into()));
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
fn nas_address_preserves_literal_address_families() {
    let literal = Url::parse("https://[fd7a:115c:a1e0::5]:8443").unwrap();
    assert_eq!(
        nas_addresses(&literal, &SystemProcessRunner),
        vec!["fd7a:115c:a1e0::5".parse::<IpAddr>().unwrap()]
    );
    let v4 = Url::parse("https://192.168.1.231:8443").unwrap();
    assert_eq!(
        nas_addresses(&v4, &SystemProcessRunner),
        vec!["192.168.1.231".parse::<IpAddr>().unwrap()]
    );
}

struct RouteRunner<'a> {
    output: &'a str,
    success: bool,
}

impl ProcessRunner for RouteRunner<'_> {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<crate::process::ProcessOutput, crate::process::ProcessError> {
        assert_eq!(program, Program::Ip);
        assert_eq!(&arguments[..3], &["-o", "route", "get"]);
        assert!(timeout <= Duration::from_millis(500));
        Ok(crate::process::ProcessOutput {
            success: self.success,
            stdout: self.output.as_bytes().to_vec(),
            stderr: vec![],
        })
    }
}

#[test]
fn policy_routes_override_the_wifi_main_table_for_both_families() {
    // Wrong implementation: a main-table Wi-Fi default hides table 52's
    // Tailscale route, or IPv6 is treated as missing IPv4 evidence.
    for (address, output, expected) in [
        (
            "100.64.0.5",
            "100.64.0.5 dev tailscale0 table 52 src 100.64.0.2",
            "tailscale0",
        ),
        (
            "fd7a:115c:a1e0::5",
            "fd7a:115c:a1e0::5 dev tailscale0 table 52",
            "tailscale0",
        ),
        (
            "2001:db8::5",
            "2001:db8::5 via fe80::1 dev enP7s7",
            "enP7s7",
        ),
        ("2001:db8::5", "2001:db8::5 dev wlP9s9", "wlP9s9"),
    ] {
        let runner = RouteRunner {
            output,
            success: true,
        };
        assert_eq!(
            selected_route(&runner, address.parse().unwrap()).as_deref(),
            Some(expected)
        );
    }
}

#[test]
fn failed_or_unparseable_route_is_unknown_and_a_fresh_probe_recovers() {
    let address = "100.64.0.5".parse().unwrap();
    for (output, success) in [("100.64.0.5 dev wlP9s9", false), ("malformed", true)] {
        assert_eq!(
            selected_route(&RouteRunner { output, success }, address),
            None
        );
        assert_eq!(
            selected_route(
                &RouteRunner {
                    output: "100.64.0.5 dev tailscale0",
                    success: true
                },
                address
            )
            .as_deref(),
            Some("tailscale0")
        );
    }
}

#[tokio::test]
async fn slow_optional_probe_returns_unknown_then_a_new_inventory_probe_succeeds() {
    // Wrong implementation: awaiting the blocking probe without a deadline
    // stalls mandatory inventory. Release the worker after proving timeout.
    static SLOT: tokio::sync::Semaphore = tokio::sync::Semaphore::const_new(1);
    let (release, wait) = std::sync::mpsc::channel();
    let evidence = bounded_probe_with_slot(
        move || {
            wait.recv_timeout(Duration::from_secs(1)).unwrap();
            NetworkEvidence {
                interfaces: Some(vec![]),
                nas_route_interface: None,
            }
        },
        Duration::from_millis(10),
        &SLOT,
    )
    .await;
    assert_eq!(evidence, NetworkEvidence::default());
    // A new mandatory inventory gets unknown immediately; no second blocked
    // worker is launched while the old optional read is still outstanding.
    assert_eq!(
        bounded_probe_with_slot(|| panic!("duplicate worker"), Duration::from_secs(1), &SLOT).await,
        NetworkEvidence::default()
    );
    release.send(()).unwrap();
    let permit = tokio::time::timeout(Duration::from_secs(1), SLOT.acquire())
        .await
        .unwrap()
        .unwrap();
    drop(permit);
    assert_eq!(
        bounded_probe_with_slot(
            || NetworkEvidence {
                interfaces: Some(vec![]),
                nas_route_interface: None
            },
            Duration::from_secs(1),
            &SLOT
        )
        .await
        .interfaces,
        Some(vec![])
    );
}

struct DnsRouteRunner {
    ipv6_route: &'static str,
}

impl ProcessRunner for DnsRouteRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<crate::process::ProcessOutput, crate::process::ProcessError> {
        assert!(timeout <= Duration::from_millis(500));
        let text = match program {
            Program::Getent => "192.168.1.20 STREAM nas\n2001:db8::20 STREAM nas\n",
            Program::Ip if arguments[3].contains(':') => self.ipv6_route,
            Program::Ip => "192.168.1.20 dev wlP9s9",
            _ => panic!("unexpected probe"),
        };
        Ok(crate::process::ProcessOutput {
            success: true,
            stdout: text.as_bytes().to_vec(),
            stderr: vec![],
        })
    }
}

#[test]
fn dual_stack_dns_never_guesses_wifi_when_ipv6_uses_wired_or_tailnet() {
    let url = Url::parse("https://nas.example:8443").unwrap();
    for ipv6_route in ["2001:db8::20 dev enP7s7", "2001:db8::20 dev tailscale0"] {
        assert_eq!(nas_route(&url, &DnsRouteRunner { ipv6_route }), None);
    }
    // Fresh observations that agree still report a genuine Wi-Fi route.
    assert_eq!(
        nas_route(
            &url,
            &DnsRouteRunner {
                ipv6_route: "2001:db8::20 dev wlP9s9"
            }
        )
        .as_deref(),
        Some("wlP9s9")
    );
}

struct UnavailableRunner;

impl ProcessRunner for UnavailableRunner {
    fn run(
        &self,
        _: Program,
        _: &[String],
        timeout: Duration,
    ) -> Result<crate::process::ProcessOutput, crate::process::ProcessError> {
        assert!(timeout <= Duration::from_millis(500));
        Err(crate::process::ProcessError::Timeout)
    }
}

#[test]
fn dns_and_route_timeouts_are_unknown_and_a_fresh_request_recovers() {
    assert_eq!(
        nas_route(
            &Url::parse("https://nas.example").unwrap(),
            &UnavailableRunner
        ),
        None
    );
    assert_eq!(
        nas_route(
            &Url::parse("https://[fd7a:115c:a1e0::5]").unwrap(),
            &UnavailableRunner
        ),
        None
    );
    assert_eq!(
        nas_route(
            &Url::parse("https://nas.example").unwrap(),
            &DnsRouteRunner {
                ipv6_route: "2001:db8::20 dev wlP9s9"
            }
        )
        .as_deref(),
        Some("wlP9s9")
    );
}

#[tokio::test]
async fn a_panicked_probe_is_unknown_and_the_next_probe_succeeds() {
    assert_eq!(
        bounded_probe(|| panic!("optional probe fault"), Duration::from_secs(1)).await,
        NetworkEvidence::default()
    );
    assert_eq!(
        bounded_probe(
            || NetworkEvidence {
                interfaces: Some(vec![]),
                nas_route_interface: None
            },
            Duration::from_secs(1)
        )
        .await
        .interfaces,
        Some(vec![])
    );
}
