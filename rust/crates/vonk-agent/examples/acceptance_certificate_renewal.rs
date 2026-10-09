//! Hosted acceptance peer; never installed as an operator renewal command.
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    os::unix::fs::OpenOptionsExt,
    path::PathBuf,
};

use chrono::Utc;
use sha2::{Digest, Sha256};
use vonk_agent::{
    client::AgentHttpClient,
    config::AgentConfig,
    identity::{active_identity_paths, renewal_time},
    rotation::rotate_if_due_at,
    runtime_identity::PreparedRuntimeIdentity,
};
use vonk_agent_protocol::generated::{
    NativeRenewalAction, NativeRenewalClock, NativeRenewalEvidence, NativeRenewalRequest,
};
use x509_parser::{parse_x509_certificate, pem::parse_x509_pem};

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
    if args.len() != 5 {
        return Err(
            "expected config, installed agent path, binary SHA256, build digest and observation mode".into(),
        );
    }
    let mode: NativeRenewalAction =
        serde_json::from_value(serde_json::Value::String(args[4].clone()))?;
    let request = NativeRenewalRequest {
        action: mode,
        config_path: args[0].clone(),
        agent_path: args[1].clone(),
        binary_sha256: args[2].clone(),
        build_digest: args[3].clone(),
    };
    let config = AgentConfig::load(&PathBuf::from(&request.config_path))?;
    let installed = PreparedRuntimeIdentity::from_executable(&PathBuf::from(&request.agent_path))?;
    if installed.binary_digest != request.binary_sha256
        || installed.build_digest != request.build_digest
    {
        return Err(
            "installed candidate agent identity does not match the exact helper build".into(),
        );
    }
    let root = config.data_dir.join("credentials");
    let journal = root.join("acceptance-renewal.json");
    let mut evidence = if mode == NativeRenewalAction::Observe {
        // Observation never calls rotation, even if the original response was lost.
        serde_json::from_slice::<NativeRenewalEvidence>(&fs::read(&journal)?)?
    } else if mode == NativeRenewalAction::Renew {
        let before = certificate_evidence(&active_identity_paths(&root)?.certificate)?;
        let wall_now = Utc::now();
        let scheduling_now = renewal_time(&root)?;
        let source = NativeRenewalEvidence {
            scheduling_clock: NativeRenewalClock::CertificateDerivedControlledClock,
            wall_clock_utc: wall_now.to_rfc3339(),
            scheduling_clock_utc: scheduling_now.to_rfc3339(),
            source_agent_binary_sha256: installed.binary_digest.clone(),
            source_agent_build_digest: installed.build_digest.clone(),
            source_certificate_sha256: before.0,
            source_public_key_sha256: before.1,
            source_lifetime_seconds: before.2,
            replacement_certificate_sha256: None,
            replacement_public_key_sha256: None,
            replacement_lifetime_seconds: None,
        };
        // Persist the exact observation identity before the only rotation call.
        let temporary = root.join(format!("acceptance-renewal.{}.tmp", uuid::Uuid::new_v4()));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(&serde_json::to_vec(&source)?)?;
        file.sync_all()?;
        fs::rename(temporary, &journal)?;
        File::open(&root)?.sync_all()?;
        let client = AgentHttpClient::from_config(&config)?;
        rotate_if_due_at(&config, &client, scheduling_now).await?;
        source
    } else {
        return Err("invalid renewal observation mode".into());
    };
    if evidence.source_agent_binary_sha256 != installed.binary_digest
        || evidence.source_agent_build_digest != installed.build_digest
    {
        return Err("renewal journal candidate identity differs".into());
    }
    let after = certificate_evidence(&active_identity_paths(&root)?.certificate)?;
    if after.0 != evidence.source_certificate_sha256 {
        evidence.replacement_certificate_sha256 = Some(after.0);
        evidence.replacement_public_key_sha256 = Some(after.1);
        evidence.replacement_lifetime_seconds = Some(after.2);
    }
    println!("{}", serde_json::to_string(&evidence)?);
    Ok(())
}
