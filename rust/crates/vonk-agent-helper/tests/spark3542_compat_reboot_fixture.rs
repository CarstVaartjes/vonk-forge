//! Native-only proof for the one pinned Spark3542 recovery reboot grant.
//!
//! This is intentionally not a utility: it has no arguments, accepts no key or
//! operation input, and signs only ScheduleReboot(60s) with a public test
//! seed. The systemd lifecycle harness runs the ignored test against its
//! disposable dev335 helper/socket fixture.

use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use ring::signature::{Ed25519KeyPair, KeyPair};
use uuid::Uuid;
use vonk_agent_helper::protocol::{
    GrantClaims, GrantSignature, HostOperation, SignedGrant, canonical_signing_bytes,
};
use vonk_agent_protocol::canonical_json;

const SOCKET: &str = "/run/vonk-forge-package-helper/package-helper.sock";
const NODE_ID: &str = "spk_2818d189042b4c77aefa7796f4befd23";
const REQUEST_ID: &str = "35420000-0000-4000-8000-000000000001";
const COLLISION_REQUEST_ID: &str = "35420000-0000-4000-8000-000000000002";
const TEST_SEED: [u8; 32] = [b'm'; 32];
const MAX_FRAME: usize = 256 * 1024;

fn send(body: &[u8], lose_response: bool) -> Option<Vec<u8>> {
    let mut stream = UnixStream::connect(SOCKET).expect("dev335 helper socket");
    stream
        .set_read_timeout(Some(Duration::from_secs(30)))
        .unwrap();
    stream
        .write_all(&(body.len() as u32).to_be_bytes())
        .unwrap();
    stream.write_all(body).unwrap();
    stream.flush().unwrap();
    if lose_response {
        return None;
    }
    let mut header = [0_u8; 4];
    stream.read_exact(&mut header).unwrap();
    let length = u32::from_be_bytes(header) as usize;
    assert!((1..=MAX_FRAME).contains(&length));
    let mut response = vec![0_u8; length];
    stream.read_exact(&mut response).unwrap();
    Some(response)
}

fn pinned_grant(request_id: &str) -> Vec<u8> {
    assert!(matches!(request_id, REQUEST_ID | COLLISION_REQUEST_ID));
    let key_pair = Ed25519KeyPair::from_seed_unchecked(&TEST_SEED).unwrap();
    assert_eq!(
        hex::encode(key_pair.public_key().as_ref()),
        "8b237d788e8eaaef550c6d125823fa45f1fd5fc29b2c88bdf871119471fc1312"
    );
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let claims = GrantClaims {
        schema_version: 1,
        authority: "vonk.host-maintenance-helper".to_owned(),
        request_id: Uuid::parse_str(request_id).unwrap(),
        node_id: NODE_ID.to_owned(),
        issued_at: now,
        expires_at: now + 30,
        operation: HostOperation::ScheduleReboot { delay_seconds: 60 },
    };
    let signature = key_pair.sign(&canonical_signing_bytes(&claims).unwrap());
    canonical_json(&SignedGrant {
        schema_version: 1,
        claims,
        signature: GrantSignature {
            algorithm: "ed25519".to_owned(),
            key_id: vonk_agent_protocol::hex_sha256(key_pair.public_key().as_ref()),
            value: hex::encode(signature.as_ref()),
        },
    })
    .unwrap()
}

#[test]
#[ignore = "requires the disposable root/systemd dev335 recovery fixture"]
fn transient_reboot_unit_collision_fails_closed_for_the_second_pinned_request() {
    assert_eq!(
        std::env::var("VONK_SPARK3542_COMPAT_FIXTURE").as_deref(),
        Ok("1")
    );
    let response = send(&pinned_grant(COLLISION_REQUEST_ID), false).unwrap();
    let value: serde_json::Value = serde_json::from_slice(&response).unwrap();
    assert_eq!(value["status"], "rejected");
}

#[test]
#[ignore = "requires the disposable root/systemd dev335 recovery fixture"]
fn sends_only_one_pinned_reboot_and_replay_is_rejected() {
    assert_eq!(
        std::env::var("VONK_SPARK3542_COMPAT_FIXTURE").as_deref(),
        Ok("1")
    );
    let body = pinned_grant(REQUEST_ID);

    let response = send(&body, false).expect("dev335 helper response");
    let value: serde_json::Value = serde_json::from_slice(&response).unwrap();
    assert_eq!(value["schema_version"], 1);
    assert_eq!(value["request_id"], REQUEST_ID);
    assert_eq!(value["status"], "reboot-scheduled");
    assert_eq!(
        value["evidence_sha256"],
        "39fa9ec190eee7b6f4dff1100d6343e10918d044c75eac8f9e9a2596173f80c9"
    );
    std::thread::sleep(Duration::from_millis(200));

    // If the helper is still accepting before its delayed self-restart, the
    // same persisted grant must hit dev335's request ledger, not schedule a
    // second root action. A connection loss is also acceptable because the
    // first authorization deliberately schedules a reboot.
    if let Ok(mut stream) = UnixStream::connect(SOCKET) {
        stream
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        stream
            .write_all(&(body.len() as u32).to_be_bytes())
            .unwrap();
        stream.write_all(&body).unwrap();
        stream.flush().unwrap();
        let mut header = [0_u8; 4];
        if stream.read_exact(&mut header).is_ok() {
            let length = u32::from_be_bytes(header) as usize;
            assert!((1..=MAX_FRAME).contains(&length));
            let mut response = vec![0_u8; length];
            stream.read_exact(&mut response).unwrap();
            let value: serde_json::Value = serde_json::from_slice(&response).unwrap();
            assert_eq!(value["status"], "rejected");
        }
    }
}
