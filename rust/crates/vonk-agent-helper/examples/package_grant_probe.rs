//! Disposable ARM64 acceptance client, never packaged. It stands in for the
//! Controller and the agent: it signs one helper operation with a throwaway
//! grant key and sends it over the real helper socket, so the installed
//! helper's own authorization, custody and install path run unchanged.
//!
//! `public-key` prints the grant key the lane writes as the root key file.
//! `send NODE_ID` reads one operation as JSON on stdin and prints the reply.
//! It prints nothing when the helper closed the connection without a reply:
//! an upgrade restarts the helper that is serving the request, and the lane
//! then observes the outcome on the host instead.
#![expect(
    clippy::unwrap_used,
    reason = "example program: a failed unwrap ends the demonstration"
)]
use std::io::Read;
use std::os::unix::net::UnixStream;
use std::time::{SystemTime, UNIX_EPOCH};

use ring::signature::{Ed25519KeyPair, KeyPair};
use vonk_agent_helper::protocol::{
    AUTHORITY, GrantClaims, GrantSignature, HostOperation, SignedGrant, canonical_signing_bytes,
    read_frame, write_frame,
};
use vonk_agent_protocol::{canonical_json, hex_sha256};

const GRANT_SEED: [u8; 32] = [53; 32];
const SOCKET: &str = "/run/vonk-forge-package-helper/package-helper.sock";

fn main() -> Result<(), String> {
    let key = Ed25519KeyPair::from_seed_unchecked(&GRANT_SEED).unwrap();
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    match arguments.as_slice() {
        [mode] if mode == "public-key" => {
            println!("{}", hex::encode(key.public_key()));
            Ok(())
        }
        [mode, node_id] if mode == "send" => {
            send(&key, node_id);
            Ok(())
        }
        _ => Err("usage: package_grant_probe public-key | send NODE_ID".to_owned()),
    }
}

fn send(key: &Ed25519KeyPair, node_id: &str) {
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).unwrap();
    let operation: HostOperation = vonk_agent_protocol::parse_strict(input.as_bytes()).unwrap();
    let issued_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
        .cast_signed();
    let claims = GrantClaims {
        schema_version: 1,
        authority: AUTHORITY.to_owned(),
        request_id: uuid::Uuid::new_v4(),
        node_id: node_id.to_owned(),
        issued_at,
        expires_at: issued_at + 120,
        operation,
    };
    let signature = key.sign(&canonical_signing_bytes(&claims).unwrap());
    let grant = SignedGrant {
        schema_version: 1,
        claims,
        signature: GrantSignature {
            algorithm: "ed25519".to_owned(),
            key_id: hex_sha256(key.public_key().as_ref()),
            value: hex::encode(signature.as_ref()),
        },
    };
    let mut stream = UnixStream::connect(SOCKET).unwrap();
    write_frame(&mut stream, &canonical_json(&grant).unwrap()).unwrap();
    match read_frame(&mut stream) {
        Ok(response) => println!("{}", String::from_utf8_lossy(&response)),
        Err(error) => eprintln!("helper closed without a reply: {error}"),
    }
}
