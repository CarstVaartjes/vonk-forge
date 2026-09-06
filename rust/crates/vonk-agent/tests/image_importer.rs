#![forbid(unsafe_code)]

use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
};

use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::image_importer::ImageImporter;
use vonk_agent_protocol::{RecipeImageImportRequest, hex_sha256};

#[test]
fn exact_layout_is_verified_before_requesting_host_import() {
    let root = tempdir().unwrap();
    let archive = root.path().join("image.docker.tar");
    fs::write(&archive, b"exact oci layout").unwrap();
    let request = RecipeImageImportRequest {
        build_id: Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap(),
        image_bytes: 16,
        image_digest: format!("sha256:{}", "d".repeat(64)),
        kind: "recipe.image.import.v1".to_owned(),
        mapping_generation: 1,
        mapping_id: Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap(),
        oci_layout_sha256: hex_sha256(b"exact oci layout"),
        schema_version: 1,
        source_node_id: format!("spk_{}", "1".repeat(32)),
    };
    let evidence = ImageImporter {
        data_root: root.path(),
    }
    .verify(&request, &archive)
    .unwrap();

    assert_eq!(evidence.oci_layout_sha256, request.oci_layout_sha256);
    assert_eq!(
        ImageImporter {
            data_root: root.path()
        }
        .runtime_arguments(&request, &archive),
        vec![
            archive.display().to_string(),
            request.oci_layout_sha256.clone(),
            "16".to_owned(),
            request.image_digest.clone(),
            request.image_digest.clone(),
            "localhost/vonk/recipe-build-00000000-0000-4000-8000-000000000001@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd".to_owned(),
        ]
    );
}

#[test]
fn staging_uses_the_docker_load_archive_name() {
    let root = tempdir().unwrap();
    let operation_id = Uuid::parse_str("00000000-0000-4000-8000-000000000009").unwrap();

    let archive = ImageImporter {
        data_root: root.path(),
    }
    .staging_path(operation_id)
    .unwrap();

    assert_eq!(
        archive,
        root.path()
            .join("image-imports")
            .join(operation_id.to_string())
            .join("image.docker.tar")
    );
}

#[test]
fn changed_archive_is_rejected_before_host_authority() {
    let root = tempdir().unwrap();
    let archive = root.path().join("image.docker.tar");
    fs::write(&archive, b"changed").unwrap();
    let request = RecipeImageImportRequest {
        build_id: Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap(),
        image_bytes: 7,
        image_digest: format!("sha256:{}", "d".repeat(64)),
        kind: "recipe.image.import.v1".to_owned(),
        mapping_generation: 1,
        mapping_id: Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap(),
        oci_layout_sha256: "e".repeat(64),
        schema_version: 1,
        source_node_id: format!("spk_{}", "1".repeat(32)),
    };
    assert!(
        ImageImporter {
            data_root: root.path(),
        }
        .verify(&request, &archive)
        .is_err()
    );
}

#[test]
fn verified_archive_is_reused_by_its_immutable_digest() {
    let root = tempdir().unwrap();
    let archive = root.path().join("image.docker.tar");
    let payload = b"shared image archive";
    fs::write(&archive, payload).unwrap();
    let request = RecipeImageImportRequest {
        build_id: Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap(),
        image_bytes: payload.len() as u64,
        image_digest: format!("sha256:{}", "d".repeat(64)),
        kind: "recipe.image.import.v1".to_owned(),
        mapping_generation: 1,
        mapping_id: Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap(),
        oci_layout_sha256: hex_sha256(payload),
        schema_version: 1,
        source_node_id: format!("spk_{}", "1".repeat(32)),
    };
    let importer = ImageImporter {
        data_root: root.path(),
    };
    let cached = importer
        .retain_verified_archive(&request, &archive)
        .unwrap();
    assert_eq!(
        importer.verified_cached_archive(&request).unwrap(),
        Some(cached)
    );
}

#[test]
fn trusted_distribution_retention_reuses_final_metadata_without_hashing() {
    let root = tempdir().unwrap();
    let digest = "a".repeat(64);
    let archive = root.path().join("oci-archives").join(&digest);
    fs::create_dir_all(archive.parent().unwrap()).unwrap();
    fs::write(&archive, b"payload with a different digest").unwrap();
    fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();

    let retained = ImageImporter {
        data_root: root.path(),
    }
    .retain_verified_distribution_archive(
        &digest,
        &format!("sha256:{}", "b".repeat(64)),
        fs::metadata(&archive).unwrap().len(),
        &archive,
    )
    .unwrap();

    assert_eq!(retained, archive);
}

#[test]
fn trusted_distribution_retention_renames_and_rejects_symlink_sources() {
    let root = tempdir().unwrap();
    let digest = "c".repeat(64);
    let source = root.path().join("distribution").join(&digest);
    let destination = root.path().join("oci-archives").join(&digest);
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    fs::create_dir_all(destination.parent().unwrap()).unwrap();
    fs::write(&source, b"trusted payload").unwrap();
    fs::set_permissions(&source, fs::Permissions::from_mode(0o600)).unwrap();
    let importer = ImageImporter {
        data_root: root.path(),
    };
    importer
        .retain_verified_distribution_archive(
            &digest,
            &format!("sha256:{}", "d".repeat(64)),
            15,
            &source,
        )
        .unwrap();
    assert!(!source.exists());
    assert!(destination.is_file());

    let symlink_source = root.path().join("distribution").join("symlink");
    symlink(&destination, &symlink_source).unwrap();
    assert!(
        importer
            .retain_verified_distribution_archive(
                &"e".repeat(64),
                &format!("sha256:{}", "f".repeat(64)),
                15,
                &symlink_source,
            )
            .is_err()
    );
}
