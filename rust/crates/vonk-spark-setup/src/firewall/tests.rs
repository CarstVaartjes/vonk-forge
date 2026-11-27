#![cfg(test)]
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
                (Some(IP_PATH), "-j -4 route get 10.0.0.231") => {
                    r#"[{"dst":"10.0.0.231","dev":"enP7s7","prefsrc":"10.0.0.211"}]"#.to_owned()
                }
                (Some(RDMA_PATH), "-j link show") => {
                    r#"[{"ifname":"rocep1s0f1","state":"ACTIVE","netdev":"enp1s0f1np1"},{"ifname":"rocep1s0f0","state":"DOWN","netdev":"enp1s0f0np0"}]"#.to_owned()
                }
                (Some(IP_PATH), "-j -4 addr show") => format!(
                    r#"[{{"ifname":"lo","flags":["LOOPBACK","UP","LOWER_UP"],"addr_info":[{{"local":"127.0.0.1","prefixlen":8,"scope":"host"}}]}},
                        {{"ifname":"enP7s7","flags":["UP","LOWER_UP"],"addr_info":[{{"local":"10.0.0.211","prefixlen":24,"scope":"global"}}]}},
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

const CANONICAL_FIREWALL: &str = "VONK_NAS_MANAGEMENT_IP=10.0.0.231\nVONK_NODE_MANAGEMENT_IP=10.0.0.211\nVONK_NODE_FABRIC_IP=192.168.100.10\nVONK_PEER_FABRIC_IP=192.168.100.11\nVONK_ENDPOINT_HOST_PORTS=8000,8101\nVONK_HOST_ENDPOINT_PORTS=8888\nVONK_RENDEZVOUS_PORT=29500\n";

#[test]
fn spark_addresses_are_detected_without_prompting() {
    let mut prompt = Values(VecDeque::new());
    let config = FirewallConfig::collect(
            &FirewallInputs::default(),
            Some("10.0.0.231".parse().unwrap()),
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
        Some("10.0.0.231".parse().unwrap()),
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
            Some("10.0.0.231".parse().unwrap()),
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
            "10.0.0.231",
            "10.0.0.211",
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
        nas_management_ip: Some("10.0.0.231".to_owned()),
        node_management_ip: Some("10.0.0.211".to_owned()),
        node_fabric_ip: Some("192.168.100.10".to_owned()),
        peer_fabric_ip: Some("192.168.100.10".to_owned()),
    };
    let mut prompt = Values(VecDeque::new());
    assert!(matches!(
        FirewallConfig::collect(&base, None, &mut prompt, &mut NoHost),
        Err(SetupError::UnsafeInput("Spark network topology"))
    ));
}
