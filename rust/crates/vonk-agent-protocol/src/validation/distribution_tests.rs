#![cfg(test)]
use super::*;

fn assignment() -> DistributionAssignment {
    DistributionAssignment {
        objects: vec![DistributionObject {
            name: "weights/model.bin".to_owned(),
            sha256: "d".repeat(64),
            bytes: 13,
            kind: "model".parse().unwrap(),
        }],
        oci_image_digest: "sha256:".to_owned() + &"f".repeat(64),
        oci_image_config_digest: "sha256:".to_owned() + &"e".repeat(64),
    }
}

#[test]
fn assignment_serde_round_trip_matches_python_wire_shape() {
    let value = assignment();
    value.validate().unwrap();
    let encoded = serde_json::to_value(&value).unwrap();
    let decoded: DistributionAssignment = serde_json::from_value(encoded.clone()).unwrap();
    assert_eq!(decoded, value);
    assert_eq!(serde_json::to_value(decoded).unwrap(), encoded);
}

#[test]
fn object_name_validation_matches_python_boundary() {
    let mut value = assignment();
    for name in [
        "weights/model bin",
        "__init__.py",
        "nested/UPPERCASE.bin",
        "模型.bin",
    ] {
        value.objects[0].name = name.to_owned();
        value.validate().unwrap();
    }
    value.objects[0].name = "模型 file_".repeat(64);
    value.validate().unwrap();
    value.objects[0].name = "../model.bin".to_owned();
    assert!(value.validate().is_err());
    value.objects[0].name = "model\0.bin".to_owned();
    assert!(value.validate().is_err());
}

#[test]
fn empty_model_support_object_uses_the_canonical_empty_digest() {
    let mut value = assignment();
    value.objects[0] = DistributionObject {
        name: "tokenizer_config.json".to_owned(),
        sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855".to_owned(),
        bytes: 0,
        kind: "model".parse().unwrap(),
    };
    value.validate().unwrap();
    value.objects[0].kind = "oci-archive".parse().unwrap();
    assert!(value.validate().is_err());
}
