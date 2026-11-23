#![cfg(test)]
use super::*;
use rcgen::{CertificateParams, KeyPair, PKCS_ED25519, date_time_ymd};
use tempfile::tempdir;

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

fn material(generation: u64, marker: u8) -> IdentityMaterial {
    IdentityMaterial {
        node_id: NODE_ID.to_owned(),
        private_key_pem: vec![marker, b'k'],
        certificate_pem: vec![marker, b'c'],
        chain_pem: vec![marker, b'h'],
        serial: format!("serial-{generation}"),
        fingerprint: format!("fingerprint-{generation}"),
        generation,
    }
}

pub(super) fn certificate_material(generation: u64, expired: bool) -> IdentityMaterial {
    let key = KeyPair::generate_for(&PKCS_ED25519).unwrap();
    let mut parameters = CertificateParams::default();
    parameters.not_before = date_time_ymd(2026, 8, 1);
    parameters.not_after = if expired {
        date_time_ymd(2026, 8, 2)
    } else {
        date_time_ymd(2026, 8, 4)
    };
    let certificate = parameters.self_signed(&key).unwrap();
    IdentityMaterial {
        node_id: NODE_ID.to_owned(),
        private_key_pem: key.serialize_pem().into_bytes(),
        certificate_pem: certificate.pem().into_bytes(),
        chain_pem: certificate.pem().into_bytes(),
        serial: format!("serial-{generation}"),
        fingerprint: format!("fingerprint-{generation}"),
        generation,
    }
}

#[test]
fn interrupted_pending_publication_keeps_active_authority_and_accepts_a_new_csr() {
    for name in ["pending-key.pem", "pending-csr.pem"] {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("credentials");
        persist_identity(&root, &material(1, 7)).unwrap();
        let active = fs::read(root.join("private-key.pem")).unwrap();
        fs::write(root.join(name), b"interrupted projection").unwrap();
        let fresh = prepare_pending(&root, NODE_ID).unwrap();
        assert_eq!(fs::read(root.join("private-key.pem")).unwrap(), active);
        let loaded = prepare_pending(&root, NODE_ID).unwrap();
        assert_eq!(loaded.csr_pem, fresh.csr_pem);
        assert_eq!(loaded.private_key_pem, fresh.private_key_pem);
        clear_pending(&root).unwrap();
        assert_ne!(
            prepare_pending(&root, NODE_ID).unwrap().csr_pem,
            fresh.csr_pem
        );
    }
}

#[test]
fn renewal_clock_uses_the_active_certificate_boundary_without_changing_identity() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    persist_identity(&root, &certificate_material(1, false)).unwrap();
    let path = active_identity_paths(&root).unwrap().certificate;
    let before = fs::read(&path).unwrap();
    let due = Utc.with_ymd_and_hms(2026, 8, 2, 12, 0, 0).unwrap();

    assert_eq!(renewal_time(&root).unwrap(), due);
    assert!(!renewal_due(&root, due - chrono::Duration::seconds(1)).unwrap());
    assert!(renewal_due(&root, due).unwrap());
    assert!(renewal_due(&root, due + chrono::Duration::seconds(1)).unwrap());
    assert_eq!(fs::read(path).unwrap(), before);
}

#[test]
fn a_fresh_certificate_is_valid_on_a_clock_that_lags_its_issuer() {
    // The Controller signs a certificate valid from the instant it issues
    // it; a Spark a little behind must not retire it as not yet valid.
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    persist_identity(&root, &certificate_material(1, false)).unwrap();
    stage_identity(&root, &certificate_material(2, false)).unwrap();
    let (_, paths) = staged_identity_paths(&root).unwrap().unwrap();
    let issued = Utc.with_ymd_and_hms(2026, 8, 1, 0, 0, 0).unwrap();

    for lag in [1, 5, CERTIFICATE_CLOCK_SKEW_SECONDS] {
        assert!(!identity_expired(&paths, issued - chrono::Duration::seconds(lag)).unwrap());
    }
    // A certificate issued well in this clock's future is still refused.
    let early = issued - chrono::Duration::seconds(CERTIFICATE_CLOCK_SKEW_SECONDS + 1);
    assert!(identity_expired(&paths, early).unwrap());
}

#[test]
fn expired_staged_identity_is_retired_without_changing_the_active_identity() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    persist_identity(&root, &certificate_material(1, false)).unwrap();
    stage_identity(&root, &certificate_material(2, true)).unwrap();
    persist_pending(&root, &generate_pending(NODE_ID).unwrap()).unwrap();
    let (generation, paths) = staged_identity_paths(&root).unwrap().unwrap();
    let now = Utc.with_ymd_and_hms(2026, 8, 2, 12, 0, 0).unwrap();

    assert!(identity_expired(&paths, now).unwrap());
    retire_expired_staged(&root, generation).unwrap();

    assert!(staged_identity_paths(&root).unwrap().is_none());
    assert!(root.join("expired-staged.json").is_file());
    assert!(!root.join("pending-key.pem").exists());
    assert!(!root.join("pending-csr.pem").exists());
    assert_eq!(
        active_identity_paths(&root).unwrap(),
        flat_paths(&root).unwrap(),
    );
}

#[test]
fn paired_identity_retires_stale_rotation_pointers_only_after_replacement_exists() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    stage_identity(&root, &material(3, b'o')).unwrap();
    publish_staged(&root, 3).unwrap();

    persist_identity(&root, &material(1, b'n')).unwrap();
    assert_eq!(
        fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
        vec![b'o', b'c'],
        "writing replacement material alone must not bypass the active pointer",
    );

    persist_paired_identity(&root, &material(1, b'n')).unwrap();

    assert!(!root.join("active.json").exists());
    assert_eq!(
        fs::read(root.join("pre-reenroll-active.json")).unwrap(),
        br#"{"generation":3}"#,
    );
    assert!(
        !root.join(generation_name(3)).exists(),
        "re-enrollment must retire obsolete generated credentials after the new flat identity is durable",
    );
    assert_eq!(
        fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
        vec![b'n', b'c'],
    );
}

#[test]
fn paired_identity_archives_staged_pointer_before_switching_active_identity() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    stage_identity(&root, &material(2, b'o')).unwrap();
    publish_staged(&root, 2).unwrap();
    stage_identity(&root, &material(3, b's')).unwrap();

    persist_paired_identity(&root, &material(1, b'n')).unwrap();

    assert!(!root.join("staged.json").exists());
    assert_eq!(
        fs::read(root.join("pre-reenroll-staged.json")).unwrap(),
        br#"{"generation":3}"#,
    );
    assert_eq!(
        fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
        vec![b'n', b'c'],
    );
}

#[test]
fn replacing_an_existing_non_active_generation_does_not_reuse_stale_material() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    stage_identity(&root, &material(2, b'o')).unwrap();
    stage_identity(&root, &material(2, b'n')).unwrap();

    let (_, paths) = staged_identity_paths(&root).unwrap().unwrap();
    assert_eq!(fs::read(paths.certificate).unwrap(), vec![b'n', b'c']);
    assert!(
        fs::read_dir(root.join("retired-generations"))
            .unwrap()
            .flatten()
            .any(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .contains("-00000000000000000002-")
                    && entry.file_name().to_string_lossy().starts_with("identity-")
            })
    );
}

#[test]
fn successful_publish_cleans_old_generation_but_retains_replacement_evidence() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    stage_identity(&root, &material(2, b'o')).unwrap();
    stage_identity(&root, &material(2, b'n')).unwrap();
    stage_identity(&root, &material(3, b't')).unwrap();
    publish_staged(&root, 3).unwrap();

    assert!(root.join(generation_name(3)).is_dir());
    assert!(!root.join(generation_name(2)).exists());
    let retired = root.join("retired-generations");
    let archive = fs::read_dir(retired)
        .unwrap()
        .flatten()
        .next()
        .unwrap()
        .path();
    assert!(
        archive
            .file_name()
            .unwrap()
            .to_string_lossy()
            .starts_with("identity-")
    );
    assert_eq!(
        fs::read(archive.join("certificate.pem")).unwrap(),
        vec![b'o', b'c']
    );
    assert_eq!(
        fs::metadata(&archive).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(
        fs::metadata(archive.join("private-key.pem"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
}

#[test]
fn retired_replacement_evidence_is_bounded() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    stage_identity(&root, &material(2, b'a')).unwrap();
    for marker in b'b'..=b'g' {
        stage_identity(&root, &material(2, marker)).unwrap();
    }

    let archives: Vec<_> = fs::read_dir(root.join("retired-generations"))
        .unwrap()
        .flatten()
        .collect();
    assert_eq!(archives.len(), MAX_RETIRED_GENERATIONS);
    assert!(archives.iter().all(|entry| {
        entry.file_name().to_string_lossy().starts_with("identity-")
            && entry.path().is_dir()
            && fs::metadata(entry.path()).unwrap().permissions().mode() & 0o777 == 0o700
    }));
}

#[test]
fn active_generation_cannot_be_replaced_by_later_staging() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    stage_identity(&root, &material(2, b'o')).unwrap();
    publish_staged(&root, 2).unwrap();

    let error = stage_identity(&root, &material(2, b'n')).unwrap_err();
    assert!(error.to_string().contains("active identity generation"));
    assert_eq!(
        fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
        vec![b'o', b'c'],
    );
}

#[test]
fn lower_generation_cannot_downgrade_the_active_identity() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    stage_identity(&root, &material(3, b'o')).unwrap();
    publish_staged(&root, 3).unwrap();

    let error = stage_identity(&root, &material(2, b'n')).unwrap_err();
    assert!(error.to_string().contains("move backwards"));
    assert_eq!(
        fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
        vec![b'o', b'c'],
    );
}

#[test]
fn flat_identity_generation_also_blocks_downgrade() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    persist_paired_identity(&root, &material(3, b'o')).unwrap();

    let error = stage_identity(&root, &material(2, b'n')).unwrap_err();
    assert!(error.to_string().contains("move backwards"));
    assert_eq!(
        fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
        vec![b'o', b'c'],
    );
}

#[test]
fn expired_chain_certificate_invalidates_a_staged_identity() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    let mut staged = certificate_material(2, false);
    staged.chain_pem = certificate_material(3, true).chain_pem;
    stage_identity(&root, &staged).unwrap();
    let (_, paths) = staged_identity_paths(&root).unwrap().unwrap();
    let now = Utc.with_ymd_and_hms(2026, 8, 2, 12, 0, 0).unwrap();

    assert!(identity_expired(&paths, now).unwrap());
}
