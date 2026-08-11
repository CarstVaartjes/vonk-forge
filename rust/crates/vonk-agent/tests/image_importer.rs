#![forbid(unsafe_code)]

use std::{cell::RefCell, fs, time::Duration};

use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::{
    image_importer::ImageImporter,
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
};
use vonk_agent_protocol::{RecipeImageImportRequest, hex_sha256};

struct Runner {
    calls: RefCell<Vec<(Program, Vec<String>)>>,
}

impl ProcessRunner for Runner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        _timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.calls.borrow_mut().push((program, arguments.to_vec()));
        Ok(ProcessOutput {
            success: true,
            stdout: if arguments.first().is_some_and(|item| item == "load") {
                format!("Loaded image: sha256:{}\n", "a".repeat(64)).into_bytes()
            } else if arguments.first().is_some_and(|item| item == "image") {
                format!("sha256:{}\tlinux\tarm64\tv1\t10001:10001\n", "d".repeat(64)).into_bytes()
            } else {
                Vec::new()
            },
            stderr: Vec::new(),
        })
    }
}

#[test]
fn exact_layout_is_verified_before_rootless_load_and_inspect() {
    let root = tempdir().unwrap();
    let archive = root.path().join("image.oci.tar");
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
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
    };

    let evidence = ImageImporter {
        runner: &runner,
        data_root: root.path(),
    }
    .import(&request, &archive)
    .unwrap();

    assert_eq!(evidence.oci_layout_sha256, request.oci_layout_sha256);
    let calls = runner.calls.borrow();
    assert_eq!(calls[0].1[0], "load");
    assert_eq!(calls[1].1[..3], ["image", "inspect", "--format"]);
    assert_eq!(
        calls[1].1.last().unwrap(),
        &format!("sha256:{}", "a".repeat(64))
    );
    assert_eq!(
        calls[2].1,
        [
            "tag",
            &format!("sha256:{}", "a".repeat(64)),
            "localhost/vonk/recipe-build-00000000-0000-4000-8000-000000000001",
        ]
    );
}

#[test]
fn changed_archive_never_reaches_podman() {
    let root = tempdir().unwrap();
    let archive = root.path().join("image.oci.tar");
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
    let runner = Runner {
        calls: RefCell::new(Vec::new()),
    };

    assert!(
        ImageImporter {
            runner: &runner,
            data_root: root.path(),
        }
        .import(&request, &archive)
        .is_err()
    );
    assert!(runner.calls.borrow().is_empty());
}
