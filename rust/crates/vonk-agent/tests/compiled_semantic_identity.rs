use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

use rcgen::{CertificateParams, KeyPair};
use sha2::{Digest, Sha256};
use vonk_agent::{
    config::AgentConfig,
    identity::{IdentityMaterial, persist_identity},
    self_test,
};

const NODE_ID: &str = "spk_00000000000000000000000000000000";

#[test]
fn cli_version_and_self_test_share_the_compiled_semantic_identity() {
    let semantic_version = env!("VONK_AGENT_SEMANTIC_VERSION");
    let agent = env!("CARGO_BIN_EXE_vonk-agent");
    let version = Command::new(agent).arg("--version").output().unwrap();
    assert!(version.status.success());
    assert_eq!(
        String::from_utf8(version.stdout).unwrap(),
        format!("vonk-agent {semantic_version}\n")
    );

    let temporary = tempfile::tempdir().unwrap();
    let data = temporary.path().join("data");
    let runtime = temporary.path().join("runtime");
    fs::create_dir(&data).unwrap();
    fs::create_dir(&runtime).unwrap();
    fs::set_permissions(&data, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    let key = KeyPair::generate().unwrap();
    let certificate = CertificateParams::new(vec!["agents.example.test".to_owned()])
        .unwrap()
        .self_signed(&key)
        .unwrap();
    let ca_path = temporary.path().join("controller-ca.pem");
    fs::write(&ca_path, certificate.pem()).unwrap();
    persist_identity(
        &data.join("credentials"),
        &IdentityMaterial {
            node_id: NODE_ID.to_owned(),
            private_key_pem: key.serialize_pem().into_bytes(),
            certificate_pem: certificate.pem().into_bytes(),
            chain_pem: certificate.pem().into_bytes(),
            serial: "1".to_owned(),
            fingerprint: hex::encode(Sha256::digest(certificate.der())),
            generation: 1,
        },
    )
    .unwrap();
    let config = AgentConfig::parse(&format!(
        "enrollment_url = \"https://enroll.example.test/\"\n\
         controller_url = \"https://agents.example.test/\"\n\
         ca_path = \"{}\"\n\
         ca_sha256 = \"{}\"\n\
         data_dir = \"{}\"\n\
         node_id = \"{NODE_ID}\"\n\
         poll_min_seconds = 2\n\
         poll_max_seconds = 60\n",
        ca_path.display(),
        hex::encode(Sha256::digest(certificate.der())),
        data.display(),
    ))
    .unwrap();

    let identity = self_test::run(&config, agent.as_ref(), &runtime).unwrap();
    assert_eq!(identity.semantic_version, semantic_version);
    assert_eq!(identity.build_digest.len(), 71);
    assert_eq!(identity.binary_digest.len(), 64);
    assert_eq!(
        identity.architecture,
        if cfg!(target_arch = "aarch64") {
            "linux-arm64"
        } else {
            "linux-amd64"
        }
    );
    assert!(identity.self_test_passed);
}
