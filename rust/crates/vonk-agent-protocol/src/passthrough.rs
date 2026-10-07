//! The one place a generated wire type becomes an untyped JSON document.
//!
//! Protocol data is typed everywhere else. The few operations that are
//! inherently about the JSON document (canonical byte form, validating a value
//! that was constructed in Rust and never deserialized, round-tripping through
//! the generated deserializer) are expressed here, on a newtype, so the rest of
//! the workspace never names `serde_json::Value`.
//! `tests/typed_wire.rs` in `vonk-agent` fails when `Value` appears elsewhere.
use serde::{Serialize, de::DeserializeOwned};
use serde_json::Value;
use std::collections::BTreeMap;

/// A generated wire value rendered as a JSON document.
///
/// Reason: canonical bytes, schema validation of directly constructed values
/// and strict round-trips are properties of the document form; the document is
/// never inspected field by field and never leaves this type.
pub struct WireDocument(Value);

impl WireDocument {
    pub fn of<T: Serialize>(value: &T) -> Result<Self, serde_json::Error> {
        serde_json::to_value(value).map(Self)
    }

    /// Whether the document is an object whose members are all null.
    pub fn is_object_of_nulls(&self) -> bool {
        self.0
            .as_object()
            .is_some_and(|object| object.values().all(Value::is_null))
    }

    /// Canonical bytes: object members in sorted key order, no whitespace.
    pub fn canonical_bytes(self) -> Result<Vec<u8>, serde_json::Error> {
        serde_json::to_vec(&sort_members(self.0))
    }

    /// Validate against the named contract model, as its deserializer would.
    pub fn validate_as(self, model: &str) -> Result<(), String> {
        crate::wire_schema::validate_constructed_document(model, &self.0)
    }

    /// Parse through the type's generated deserializer.
    pub fn into_typed<T: DeserializeOwned>(self) -> Result<T, serde_json::Error> {
        serde_json::from_value(self.0)
    }
}

/// Validate a value constructed in Rust against its named contract model.
pub fn validate_generated<T: Serialize>(model: &str, value: &T) -> Result<(), String> {
    WireDocument::of(value)
        .map_err(|_| "wire document cannot be rendered".to_owned())?
        .validate_as(model)
}

/// Round-trip a value through its generated deserializer, so a message its own
/// contract refuses never survives as a typed value.
pub fn revalidate<T: Serialize + DeserializeOwned>(value: &T) -> Result<T, serde_json::Error> {
    WireDocument::of(value)?.into_typed()
}

fn sort_members(value: Value) -> Value {
    match value {
        Value::Object(values) => Value::Object(
            values
                .into_iter()
                .map(|(key, value)| (key, sort_members(value)))
                .collect::<BTreeMap<_, _>>()
                .into_iter()
                .collect(),
        ),
        Value::Array(values) => Value::Array(values.into_iter().map(sort_members).collect()),
        other => other,
    }
}
