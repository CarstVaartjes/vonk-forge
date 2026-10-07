//! Hosted acceptance peer; never installed as an operator renewal command.
use std::{fs, path::PathBuf};

use chrono::Utc;
use serde::Serialize;
use sha2::{Digest, Sha256};
use vonk_agent::{
    client::AgentHttpClient,
    config::AgentConfig,
    identity::{active_identity_paths, renewal_time},
    rotation::rotate_if_due_at,
    runtime_identity::PreparedRuntimeIdentity,
};
use x509_parser::{parse_x509_certificate, pem::parse_x509_pem};

#[derive(Serialize)]
struct Evidence {
    scheduling_clock: &'static str,
    wall_clock_utc: String,
    scheduling_clock_utc: String,
    source_agent_binary_sha256: String,
    source_agent_build_digest: String,
    source_certificate_sha256: String,
    replacement_certificate_sha256: String,
    source_public_key_sha256: String,
    replacement_public_key_sha256: String,
    source_lifetime_seconds: i64,
    replacement_lifetime_seconds: i64,
}

fn certificate_evidence(
    path: &std::path::Path,
) -> Result<(String, String, i64), Box<dyn std::error::Error>> {
    let raw = fs::read(path)?;
    let (_, pem) = parse_x509_pem(&raw).map_err(|_| "certificate PEM is invalid")?;
    let (_, certificate) =
        parse_x509_certificate(&pem.contents).map_err(|_| "certificate DER is invalid")?;
    Ok((
        hex::encode(Sha256::digest(&pem.contents)),
        hex::encode(Sha256::digest(certificate.public_key().raw)),
        certificate.validity().not_after.timestamp()
            - certificate.validity().not_before.timestamp(),
    ))
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() != 4 {
        return Err(
            "expected config, installed agent path, binary SHA256, and build digest".into(),
        );
    }
    let config = AgentConfig::load(&PathBuf::from(&args[0]))?;
    let installed = PreparedRuntimeIdentity::from_executable(&PathBuf::from(&args[1]))?;
    if installed.binary_digest != args[2] || installed.build_digest != args[3] {
        return Err(
            "installed candidate agent identity does not match the exact helper build".into(),
        );
    }
    let root = config.data_dir.join("credentials");
    let before = certificate_evidence(&active_identity_paths(&root)?.certificate)?;
    if before.2 != 2_592_000 {
        return Err("active certificate violates the fixed thirty-day policy".into());
    }
    let wall_now = Utc::now();
    let scheduling_now = renewal_time(&root)?;
    if scheduling_now <= wall_now {
        return Err(
            "acceptance certificate is already due; controlled-clock proof is not isolated".into(),
        );
    }
    let client = AgentHttpClient::from_config(&config)?;
    if !rotate_if_due_at(&config, &client, scheduling_now).await? {
        return Err("native certificate rotation did not run".into());
    }
    let after = certificate_evidence(&active_identity_paths(&root)?.certificate)?;
    if after.2 != 2_592_000 || after.0 == before.0 || after.1 == before.1 {
        return Err("replacement did not preserve policy and rekey the actual identity".into());
    }
    println!(
        "{}",
        serde_json::to_string(&Evidence {
            scheduling_clock: "certificate-derived-controlled-clock",
            wall_clock_utc: wall_now.to_rfc3339(),
            scheduling_clock_utc: scheduling_now.to_rfc3339(),
            source_agent_binary_sha256: installed.binary_digest,
            source_agent_build_digest: installed.build_digest,
            source_certificate_sha256: before.0,
            replacement_certificate_sha256: after.0,
            source_public_key_sha256: before.1,
            replacement_public_key_sha256: after.1,
            source_lifetime_seconds: before.2,
            replacement_lifetime_seconds: after.2,
        })?
    );
    Ok(())
}
