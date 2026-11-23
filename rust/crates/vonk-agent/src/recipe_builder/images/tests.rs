#![cfg(test)]
use super::*;
use vonk_agent_protocol::RecipeBuildAdapterDefinition;

const ADAPTER_STAGE: &str = "ARG VONK_RECIPE_IMAGE\nFROM ${VONK_RECIPE_IMAGE}\nUSER 0\n\
    RUN set -eu \\\n && install --directory --owner=10001 --group=10001 /outputs \\\n \
    && test -x /opt/vonk/bin/vllm\nUSER 10001:10001\nWORKDIR /tmp\n";

fn adapter(containerfile: &str) -> RecipeBuildAdapter {
    let definition = RecipeBuildAdapterDefinition {
        adapter_id: "vonk.runtime-contract.vllm.v1".to_owned(),
        containerfile: containerfile.to_owned(),
        engine: "vllm".to_owned(),
        image_user: "10001:10001".to_owned(),
    };
    let digest = hex_sha256(&canonical_json(&definition).expect("canonical adapter"));
    RecipeBuildAdapter {
        adapter_sha256: digest,
        definition,
    }
}

fn inspect_payload(adapter: &RecipeBuildAdapter, user: &str, interface: &str) -> Vec<u8> {
    format!(
        "linux\tarm64\t{interface}\t{}\t{}\t{user}",
        adapter.definition.adapter_id, adapter.adapter_sha256
    )
    .into_bytes()
}

#[test]
fn adapter_stage_is_written_only_for_its_own_digest() {
    let directory = tempfile::tempdir().expect("temporary adapter context");
    let path = directory.path().join("Containerfile");
    let accepted = adapter(ADAPTER_STAGE);
    super::write_adapter_containerfile(&path, &accepted).expect("matching digest");
    assert_eq!(
        std::fs::read_to_string(&path).expect("written adapter"),
        ADAPTER_STAGE
    );
    // The wrong implementation trusts the declared fields without
    // re-deriving the canonical digest, so it installs a tampered stage.
    let tampered = RecipeBuildAdapter {
        adapter_sha256: "0".repeat(64),
        definition: accepted.definition.clone(),
    };
    assert!(super::write_adapter_containerfile(&path, &tampered).is_err());
    assert_eq!(std::fs::read_to_string(&path).unwrap(), ADAPTER_STAGE);
    super::write_adapter_containerfile(&path, &accepted).unwrap();
}

#[test]
fn digest_bound_adapter_is_consumed_without_a_second_syntax_policy() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("Containerfile");
    for source in [
        "FROM scratch\n",
        "FROM ${VONK_RECIPE_IMAGE}\nADD https://example.invalid/x /x\n",
    ] {
        super::write_adapter_containerfile(&path, &adapter(source)).unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), source);
    }
}

#[test]
fn adapted_image_evidence_binds_interface_adapter_and_user() {
    let request = adapter(ADAPTER_STAGE);
    super::inspect_adapted_image(&inspect_payload(&request, "10001:10001", "v1"), &request)
        .expect("matching adapted evidence");
    // The wrong implementation checks only that some label and some user
    // exist, so a different adapter or user would be accepted.
    for payload in [
        inspect_payload(&request, "0:0", "v1"),
        inspect_payload(&request, "10001:10001", "v2"),
        b"linux\tarm64\tv1\tvonk.runtime-contract.vllm.v1\t".to_vec(),
    ] {
        assert!(super::inspect_adapted_image(&payload, &request).is_err());
        super::inspect_adapted_image(&inspect_payload(&request, "10001:10001", "v1"), &request)
            .unwrap();
    }
}
