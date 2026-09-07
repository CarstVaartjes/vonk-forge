use std::io::{self, Read};

use ring::signature::Ed25519KeyPair;
use vonk_agent_helper::protocol::{
    ContainerRuntimeAction, GrantVerifier, PeerIdentity, parse_request, sign_observation_receipt,
};
use vonk_agent_protocol::{RecipeRunObservationOutcome, canonical_json};

const NOW: i64 = 2_100_000_000;
const ALLOWED_GID: u32 = 971;
const GRANT_PUBLIC_KEY: &str = "66cd608b928b88e50e0efeaa33faf1c43cefe07294b0b87e9fe0aba6a3cf7633";

fn main() {
    let mut raw = Vec::new();
    io::stdin().read_to_end(&mut raw).unwrap();
    let raw = raw.strip_suffix(b"\n").unwrap_or(&raw);
    let grant = parse_request(raw).unwrap();
    let public_key = hex::decode(GRANT_PUBLIC_KEY).unwrap();
    GrantVerifier::new(&public_key, ALLOWED_GID)
        .unwrap()
        .authorize(
            &grant,
            &PeerIdentity {
                uid: 1001,
                primary_gid: ALLOWED_GID,
                supplementary_gids: Vec::new(),
            },
            NOW + 1,
        )
        .unwrap();

    let vonk_agent_protocol::HostHelperOperation::ExecuteContainerRuntimeRequest {
        action: ContainerRuntimeAction::RunInspect,
        job_id: _,
        operation_id: _,
        attempt: _,
        fence: _,
        request_sha256,
        observation_identity_sha256: Some(observation_identity_sha256),
    } = &grant.claims.operation
    else {
        panic!("probe input is not an exact run inspection grant");
    };
    let signer = Ed25519KeyPair::from_seed_unchecked(&[23; 32]).unwrap();
    let receipt = sign_observation_receipt(
        &signer,
        &grant.claims.node_id,
        grant.claims.request_id,
        request_sha256,
        observation_identity_sha256,
        RecipeRunObservationOutcome::Running,
        NOW + 1,
    )
    .unwrap();
    println!(
        "{}",
        String::from_utf8(canonical_json(&receipt).unwrap()).unwrap()
    );
}
