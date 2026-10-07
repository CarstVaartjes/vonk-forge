use std::io::{self, Read};

use vonk_agent_helper::protocol::{
    ContainerRuntimeAction, GrantVerifier, PeerIdentity, parse_request,
};
use vonk_agent_protocol::canonical_json;

const ALLOWED_GID: u32 = 971;

fn main() {
    let mut raw = Vec::new();
    io::stdin().read_to_end(&mut raw).unwrap();
    let raw = raw.strip_suffix(b"\n").unwrap_or(&raw);
    let grant = parse_request(raw).unwrap();
    let now = std::env::var("VONK_HOST_HELPER_WIRE_NOW")
        .ok()
        .and_then(|value| value.parse::<i64>().ok())
        .unwrap_or(2_100_000_000);
    let public_key = std::env::var("VONK_HOST_HELPER_GRANT_PUBLIC_KEY").unwrap_or_else(|_| {
        "66cd608b928b88e50e0efeaa33faf1c43cefe07294b0b87e9fe0aba6a3cf7633".to_owned()
    });
    let public_key = hex::decode(public_key).unwrap();
    GrantVerifier::new(&public_key, ALLOWED_GID)
        .unwrap()
        .authorize(
            &grant,
            &PeerIdentity {
                uid: 1001,
                primary_gid: ALLOWED_GID,
                supplementary_gids: Vec::new(),
            },
            now + 1,
        )
        .unwrap();

    match &grant.claims.operation {
        vonk_agent_protocol::HostHelperOperation::ExecuteContainerRuntimeRequestOperation(
            vonk_agent_protocol::generated::ExecuteContainerRuntimeRequestOperation {
                action: ContainerRuntimeAction::Stop,
                stop_plan_sha256: Some(_),
                run_generation: Some(_),
                runtime_run_id: Some(_),
                runtime_target_id: Some(_),
                runtime_installation_id: Some(_),
                ..
            },
        ) => {
            println!(
                "{}",
                String::from_utf8(canonical_json(&grant).unwrap()).unwrap()
            );
        }
        vonk_agent_protocol::HostHelperOperation::ExecuteContainerRuntimeRequestOperation(
            vonk_agent_protocol::generated::ExecuteContainerRuntimeRequestOperation {
                action: ContainerRuntimeAction::RunInspect,
                ..
            },
        ) => {
            println!(
                "{}",
                String::from_utf8(canonical_json(&grant).unwrap()).unwrap()
            );
        }
        vonk_agent_protocol::HostHelperOperation::ExecuteContainerRuntimeRequestOperation(
            vonk_agent_protocol::generated::ExecuteContainerRuntimeRequestOperation {
                action: ContainerRuntimeAction::InstallationCleanup,
                installation_id: Some(installation_id),
                reconciliation_identity: Some(identity),
                ..
            },
        ) if identity.installation_id == *installation_id => {
            println!(
                "{}",
                String::from_utf8(canonical_json(&grant).unwrap()).unwrap()
            );
        }
        _ => panic!("probe input is not a bound run inspection, Stop or reconciliation grant"),
    }
}
