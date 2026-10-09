#![cfg(test)]

use serde_json::json;
use std::fs;

use crate::render::render;
use crate::schema::prepare;

#[test]
fn only_unbounded_integer_schemas_use_the_lossless_scalar() {
    let mut free = json!({"type":"integer"});
    prepare(&mut free);
    assert_eq!(free, json!({"type":"integer","format":"vonk-integer"}));
    let mut lower_only = json!({"type":"integer","minimum":0});
    prepare(&mut lower_only);
    assert_eq!(lower_only, free);
    let mut counter = json!({"type":"integer","minimum":0,"maximum":u64::MAX});
    prepare(&mut counter);
    assert_eq!(counter, json!({"type":"integer","format":"uint64"}));
    let mut scalar = json!({"type":"integer","minimum":i64::MIN,"maximum":i64::MAX});
    prepare(&mut scalar);
    assert_eq!(scalar, json!({"type":"integer","format":"int64"}));
}

#[test]
fn byte_representation_requires_canonical_byte_bounds() {
    let mut byte = json!({"type":"integer","format":"uint8","minimum":0,"maximum":255});
    prepare(&mut byte);
    assert_eq!(byte, json!({"type":"integer","format":"uint8"}));
    let mut wide = json!({"type":"integer","format":"uint8","minimum":0,"maximum":256});
    prepare(&mut wide);
    assert_eq!(wide, json!({"type":"integer","format":"uint32"}));
    let mut signed = json!({"type":"integer","format":"uint8","minimum":-1,"maximum":255});
    prepare(&mut signed);
    assert_eq!(signed, json!({"type":"integer","format":"int64"}));
    let mut unbounded = json!({"type":"integer","format":"uint8","minimum":0});
    prepare(&mut unbounded);
    assert_eq!(unbounded, json!({"type":"integer","format":"vonk-integer"}));
}

#[test]
fn committed_generated_types_match_the_committed_wire_schema() {
    let protocol = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../vonk-agent-protocol");
    let rendered = render(protocol.join("schema/wire.json").to_str().unwrap()).unwrap();
    let committed = fs::read_to_string(protocol.join("src/generated.rs")).unwrap();
    assert!(
        committed == rendered,
        "stale generated Rust wire types; run scripts/generate-agent-wire"
    );
}
#[test]
fn annotations_do_not_remove_identically_named_properties() {
    let mut schema = json!({"type":"object", "description":"metadata", "properties": {
        "description":{"type":"string", "maxLength":256},
        "default":{"type":"boolean"}, "title":{"type":"string"}, "not":{"type":"integer"}
    },"required":["description","default","title","not"]});
    prepare(&mut schema);
    assert!(schema.get("description").is_none());
    assert_eq!(schema["properties"]["description"]["type"], "string");
    assert_eq!(schema["properties"].as_object().unwrap().len(), 4);
    assert_eq!(schema["properties"]["not"]["type"], "integer");
}
