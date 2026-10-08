//! Firewall.

use super::*;

impl FirewallConfig {
    pub(super) fn collect(
        inputs: &FirewallInputs,
        controller_address: Option<Ipv4Addr>,
        prompt: &mut dyn Prompt,
        runner: &mut dyn CommandRunner,
    ) -> Result<Self, SetupError> {
        fn ipv4(value: &str, field: &'static str) -> Result<Ipv4Addr, SetupError> {
            value
                .parse::<Ipv4Addr>()
                .ok()
                .filter(|address| valid_site_ipv4(*address))
                .ok_or(SetupError::UnsafeInput(field))
        }

        // An explicit value wins, then this host's own detected address; the
        // operator is asked only when detection is unavailable or ambiguous.
        fn resolve(
            supplied: Option<&String>,
            detected: Option<Ipv4Addr>,
            prompt: &mut dyn Prompt,
            label: &str,
            field: &'static str,
        ) -> Result<Ipv4Addr, SetupError> {
            if let Some(value) = supplied {
                return ipv4(value, field);
            }
            if let Some(address) = detected {
                eprintln!("vonk-spark-setup: detected {field} {address}");
                return Ok(address);
            }
            ipv4(&prompt.value(label).map_err(|_| SetupError::Prompt)?, field)
        }

        let nas_management_ip = match (&inputs.nas_management_ip, controller_address) {
            (Some(value), _) => ipv4(value, "NAS management address")?,
            (None, Some(address)) => ipv4(&address.to_string(), "NAS management address")?,
            (None, None) => ipv4(
                &prompt
                    .value("NAS management IPv4 address")
                    .map_err(|_| SetupError::Prompt)?,
                "NAS management address",
            )?,
        };
        let detected = if inputs.node_management_ip.is_some()
            && inputs.node_fabric_ip.is_some()
            && inputs.peer_fabric_ip.is_some()
        {
            DetectedAddresses::default()
        } else {
            detect_spark_addresses(runner, nas_management_ip)
        };
        let node_management_ip = resolve(
            inputs.node_management_ip.as_ref(),
            detected.node_management_ip,
            prompt,
            "Spark management IPv4 address",
            "Spark management address",
        )?;
        let fabric_label = match detected.fabric_candidates.as_slice() {
            [] | [_] => "Spark fabric IPv4 address".to_owned(),
            candidates => format!(
                "Spark fabric IPv4 address (one of {})",
                candidates
                    .iter()
                    .map(Ipv4Addr::to_string)
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        };
        let node_fabric_ip = resolve(
            inputs.node_fabric_ip.as_ref(),
            detected.node_fabric_ip,
            prompt,
            &fabric_label,
            "Spark fabric address",
        )?;
        let detected_peer = if Some(node_fabric_ip) == detected.node_fabric_ip {
            detected.peer_fabric_ip
        } else {
            None
        };
        let peer_fabric_ip = resolve(
            inputs.peer_fabric_ip.as_ref(),
            detected_peer,
            prompt,
            "Peer Spark fabric IPv4 address",
            "peer Spark fabric address",
        )?;
        let ports = site_ports();
        let config = Self {
            nas_management_ip,
            node_management_ip,
            node_fabric_ip,
            peer_fabric_ip,
            endpoint_host_ports: ports.endpoint_host_ports,
            host_endpoint_ports: ports.host_endpoint_ports,
            rendezvous_port: ports.rendezvous_port,
            fabric_bandwidth_mbps: FABRIC_BANDWIDTH_MBPS,
        };
        if !config.valid() {
            return Err(SetupError::UnsafeInput("Spark network topology"));
        }
        Ok(config)
    }

    pub(super) fn valid(&self) -> bool {
        let addresses = [
            self.nas_management_ip,
            self.node_management_ip,
            self.node_fabric_ip,
            self.peer_fabric_ip,
        ];
        addresses.iter().all(|address| valid_site_ipv4(*address))
            && addresses
                .iter()
                .enumerate()
                .all(|(index, address)| !addresses[index + 1..].contains(address))
            && !self.endpoint_host_ports.is_empty()
            && valid_unique_ports(&self.endpoint_host_ports)
            && valid_unique_ports(&self.host_endpoint_ports)
            && !self.endpoint_host_ports.contains(&self.rendezvous_port)
            && !self.host_endpoint_ports.contains(&self.rendezvous_port)
            && self.rendezvous_port >= 1024
            && (1..=1_000_000).contains(&self.fabric_bandwidth_mbps)
    }

    pub(super) fn render(&self) -> String {
        let ports = |values: &[u16]| {
            values
                .iter()
                .map(u16::to_string)
                .collect::<Vec<_>>()
                .join(",")
        };
        format!(
            "VONK_NAS_MANAGEMENT_IP={}\nVONK_NODE_MANAGEMENT_IP={}\nVONK_NODE_FABRIC_IP={}\nVONK_PEER_FABRIC_IP={}\nVONK_ENDPOINT_HOST_PORTS={}\nVONK_HOST_ENDPOINT_PORTS={}\nVONK_RENDEZVOUS_PORT={}\n",
            self.nas_management_ip,
            self.node_management_ip,
            self.node_fabric_ip,
            self.peer_fabric_ip,
            ports(&self.endpoint_host_ports),
            ports(&self.host_endpoint_ports),
            self.rendezvous_port,
        )
    }
}

pub(super) fn valid_site_ipv4(value: Ipv4Addr) -> bool {
    !value.is_unspecified()
        && !value.is_loopback()
        && !value.is_link_local()
        && !value.is_multicast()
        && !value.is_broadcast()
}

pub(super) fn valid_unique_ports(values: &[u16]) -> bool {
    values.iter().all(|value| *value >= 1024)
        && values
            .iter()
            .enumerate()
            .all(|(index, value)| !values[index + 1..].contains(value))
}

#[derive(Debug, Default, PartialEq, Eq)]
pub(super) struct DetectedAddresses {
    pub(super) node_management_ip: Option<Ipv4Addr>,
    pub(super) node_fabric_ip: Option<Ipv4Addr>,
    pub(super) peer_fabric_ip: Option<Ipv4Addr>,
    pub(super) fabric_candidates: Vec<Ipv4Addr>,
}

/// Read this Spark's own addresses from the kernel. The management address is
/// the source the kernel uses to reach the NAS; the fabric address is the only
/// global IPv4 on an active RDMA (RoCE) interface other than the management
/// one; the peer is the other end of that point-to-point subnet or its only
/// known neighbour. Anything missing or ambiguous is left for the operator.
pub(super) fn detect_spark_addresses(
    runner: &mut dyn CommandRunner,
    nas: Ipv4Addr,
) -> DetectedAddresses {
    /// The fields read from iproute2's `ip -j route get`.
    #[derive(Deserialize)]
    struct RouteEntry {
        dev: Option<String>,
        prefsrc: Option<String>,
    }
    /// The fields read from `rdma -j link show`.
    #[derive(Deserialize)]
    struct RdmaLink {
        state: Option<String>,
        netdev: Option<String>,
    }
    /// The fields read from `ip -j -4 addr show`.
    #[derive(Deserialize)]
    struct InterfaceEntry {
        ifname: Option<String>,
        #[serde(default)]
        flags: Vec<String>,
        #[serde(default)]
        addr_info: Vec<AddressInfo>,
    }
    #[derive(Deserialize)]
    struct AddressInfo {
        scope: Option<String>,
        local: Option<String>,
        prefixlen: Option<u64>,
    }
    /// The fields read from `ip -j -4 neigh show`.
    #[derive(Deserialize)]
    struct NeighbourEntry {
        dst: Option<String>,
        #[serde(default)]
        state: Vec<String>,
    }
    fn json<T: serde::de::DeserializeOwned>(
        runner: &mut dyn CommandRunner,
        program: &str,
        args: &[&str],
    ) -> Vec<T> {
        runner
            .run(Command::new(program, args.iter().copied()).suppress_stderr())
            .ok()
            .filter(|output| output.success)
            .and_then(|output| serde_json::from_slice(&output.stdout).ok())
            .unwrap_or_default()
    }
    fn site_ipv4(value: Option<&str>) -> Option<Ipv4Addr> {
        value?
            .parse::<Ipv4Addr>()
            .ok()
            .filter(|address| valid_site_ipv4(*address))
    }

    let mut detected = DetectedAddresses::default();
    let nas = nas.to_string();
    let route: Vec<RouteEntry> = json(runner, IP_PATH, &["-j", "-4", "route", "get", &nas]);
    let Some(route) = route.first() else {
        return detected;
    };
    let Some(management_device) = route.dev.clone() else {
        return detected;
    };
    detected.node_management_ip = site_ipv4(route.prefsrc.as_deref());

    let rdma_devices = json::<RdmaLink>(runner, RDMA_PATH, &["-j", "link", "show"])
        .into_iter()
        .filter(|link| link.state.as_deref() == Some("ACTIVE"))
        .filter_map(|link| link.netdev)
        .collect::<Vec<_>>();
    let mut fabric = Vec::new();
    for interface in json::<InterfaceEntry>(runner, IP_PATH, &["-j", "-4", "addr", "show"]) {
        let Some(name) = interface.ifname.as_deref() else {
            continue;
        };
        let up = interface.flags.iter().any(|flag| flag == "LOWER_UP");
        if name == management_device || !up || !rdma_devices.iter().any(|device| device == name) {
            continue;
        }
        for address in &interface.addr_info {
            let global = address.scope.as_deref() == Some("global");
            let prefix = address.prefixlen;
            if let (true, Some(local), Some(prefix)) =
                (global, site_ipv4(address.local.as_deref()), prefix)
                && !fabric.iter().any(|(existing, _, _)| *existing == local)
            {
                fabric.push((local, name.to_owned(), prefix));
            }
        }
    }
    detected.fabric_candidates = fabric.iter().map(|(address, _, _)| *address).collect();
    let [(local, device, prefix)] = fabric.as_slice() else {
        return detected;
    };
    detected.node_fabric_ip = Some(*local);
    let bits = u32::from(*local);
    detected.peer_fabric_ip = match prefix {
        31 => Some(Ipv4Addr::from(bits ^ 1)),
        30 if bits & 3 == 1 || bits & 3 == 2 => Some(Ipv4Addr::from(bits ^ 3)),
        _ => {
            let neighbours = json::<NeighbourEntry>(
                runner,
                IP_PATH,
                &["-j", "-4", "neigh", "show", "dev", device],
            )
            .into_iter()
            .filter(|neighbour| {
                !neighbour
                    .state
                    .iter()
                    .any(|state| state == "FAILED" || state == "INCOMPLETE")
            })
            .filter_map(|neighbour| site_ipv4(neighbour.dst.as_deref()))
            .filter(|address| address != local)
            .collect::<Vec<_>>();
            match neighbours.as_slice() {
                [peer] => Some(*peer),
                _ => None,
            }
        }
    }
    .filter(|peer| valid_site_ipv4(*peer) && peer != local);
    detected
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    struct Values(VecDeque<String>);

    impl Prompt for Values {
        fn value(&mut self, _label: &str) -> Result<String, String> {
            self.0.pop_front().ok_or_else(|| "missing value".to_owned())
        }

        fn secret(&mut self, _label: &str) -> Result<String, String> {
            Err("unexpected secret prompt".to_owned())
        }
    }

    /// Answers the kernel queries of a Spark with one management link and
    /// one active RoCE fabric link; every other command fails.
    struct SparkHost {
        fabric: &'static str,
        neighbours: &'static str,
    }

    impl CommandRunner for SparkHost {
        fn run(&mut self, command: Command) -> Result<CommandOutput, String> {
            let args = command.args.join(" ");
            let stdout = match (command.program.to_str(), args.as_str()) {
                (Some(IP_PATH), "-j -4 route get 192.168.1.231") => {
                    r#"[{"dst":"192.168.1.231","dev":"enP7s7","prefsrc":"192.168.1.211"}]"#.to_owned()
                }
                (Some(RDMA_PATH), "-j link show") => {
                    r#"[{"ifname":"rocep1s0f1","state":"ACTIVE","netdev":"enp1s0f1np1"},{"ifname":"rocep1s0f0","state":"DOWN","netdev":"enp1s0f0np0"}]"#.to_owned()
                }
                (Some(IP_PATH), "-j -4 addr show") => format!(
                    r#"[{{"ifname":"lo","flags":["LOOPBACK","UP","LOWER_UP"],"addr_info":[{{"local":"127.0.0.1","prefixlen":8,"scope":"host"}}]}},
                        {{"ifname":"enP7s7","flags":["UP","LOWER_UP"],"addr_info":[{{"local":"192.168.1.211","prefixlen":24,"scope":"global"}}]}},
                        {{"ifname":"docker0","flags":["UP","LOWER_UP"],"addr_info":[{{"local":"172.17.0.1","prefixlen":16,"scope":"global"}}]}},
                        {{"ifname":"enp1s0f1np1","flags":["UP","LOWER_UP"],"addr_info":[{}]}}]"#,
                    self.fabric
                ),
                (Some(IP_PATH), "-j -4 neigh show dev enp1s0f1np1") => self.neighbours.to_owned(),
                _ => return Err("unavailable".to_owned()),
            };
            Ok(CommandOutput::success(stdout.into_bytes()))
        }

        fn authenticate_sudo(&mut self, _sudo: &Path) -> Result<(), SetupError> {
            Ok(())
        }
    }

    struct NoHost;

    impl CommandRunner for NoHost {
        fn run(&mut self, _command: Command) -> Result<CommandOutput, String> {
            Err("unavailable".to_owned())
        }

        fn authenticate_sudo(&mut self, _sudo: &Path) -> Result<(), SetupError> {
            Ok(())
        }
    }

    const CANONICAL_FIREWALL: &str = "VONK_NAS_MANAGEMENT_IP=192.168.1.231\nVONK_NODE_MANAGEMENT_IP=192.168.1.211\nVONK_NODE_FABRIC_IP=192.168.100.10\nVONK_PEER_FABRIC_IP=192.168.100.11\nVONK_ENDPOINT_HOST_PORTS=8000,8101\nVONK_HOST_ENDPOINT_PORTS=8888\nVONK_RENDEZVOUS_PORT=29500\n";

    #[test]
    fn spark_addresses_are_detected_without_prompting() {
        let mut prompt = Values(VecDeque::new());
        let config = FirewallConfig::collect(
            &FirewallInputs::default(),
            Some("192.168.1.231".parse().unwrap()),
            &mut prompt,
            &mut SparkHost {
                fabric: r#"{"local":"192.168.100.10","prefixlen":24,"scope":"global"}"#,
                neighbours: r#"[{"dst":"192.168.100.11","state":["STALE"]},{"dst":"192.168.100.12","state":["FAILED"]}]"#,
            },
        )
        .unwrap();
        assert_eq!(config.render(), CANONICAL_FIREWALL);
        assert_eq!(config.fabric_bandwidth_mbps, 200_000);

        // A point-to-point fabric subnet names the peer without neighbours.
        let config = FirewallConfig::collect(
            &FirewallInputs::default(),
            Some("192.168.1.231".parse().unwrap()),
            &mut prompt,
            &mut SparkHost {
                fabric: r#"{"local":"192.168.100.10","prefixlen":31,"scope":"global"}"#,
                neighbours: "[]",
            },
        )
        .unwrap();
        assert_eq!(config.render(), CANONICAL_FIREWALL);
    }

    #[test]
    fn ambiguous_or_undetectable_addresses_fall_back_to_prompts() {
        // Two fabric addresses and no known peer: ask for both.
        let mut prompt = Values(
            ["192.168.100.10", "192.168.100.11"]
                .map(str::to_owned)
                .into(),
        );
        let config = FirewallConfig::collect(
            &FirewallInputs::default(),
            Some("192.168.1.231".parse().unwrap()),
            &mut prompt,
            &mut SparkHost {
                fabric: r#"{"local":"192.168.100.10","prefixlen":24,"scope":"global"},{"local":"192.168.101.10","prefixlen":24,"scope":"global"}"#,
                neighbours: "[]",
            },
        )
        .unwrap();
        assert_eq!(config.render(), CANONICAL_FIREWALL);
        assert!(prompt.0.is_empty());

        let mut prompt = Values(
            [
                "192.168.1.231",
                "192.168.1.211",
                "192.168.100.10",
                "192.168.100.11",
            ]
            .map(str::to_owned)
            .into(),
        );
        let config =
            FirewallConfig::collect(&FirewallInputs::default(), None, &mut prompt, &mut NoHost)
                .unwrap();
        assert_eq!(config.render(), CANONICAL_FIREWALL);
        assert!(prompt.0.is_empty());
    }

    #[test]
    fn firewall_configuration_rejects_ambiguous_topology() {
        let base = FirewallInputs {
            nas_management_ip: Some("192.168.1.231".to_owned()),
            node_management_ip: Some("192.168.1.211".to_owned()),
            node_fabric_ip: Some("192.168.100.10".to_owned()),
            peer_fabric_ip: Some("192.168.100.10".to_owned()),
        };
        let mut prompt = Values(VecDeque::new());
        assert!(matches!(
            FirewallConfig::collect(&base, None, &mut prompt, &mut NoHost),
            Err(SetupError::UnsafeInput("Spark network topology"))
        ));
    }
}
