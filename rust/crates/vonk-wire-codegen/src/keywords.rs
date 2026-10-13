//! Every keyword of the exported Pydantic schema must be honoured in Rust.
//!
//! A keyword is either translated into the generated declarations, enforced by
//! the generated (de)serialization path, which validates each document against
//! this exact schema (`vonk-agent-protocol/src/wire_schema.rs`), or an
//! annotation without effect on which documents are valid. Rendering fails on
//! any other keyword, or on a `format` the validator does not check, so a
//! Python-side constraint cannot silently disappear on the Rust side.
use serde_json::Value;

/// Shapes typify translates into Rust declarations.
const TRANSLATED: &[&str] = &[
    "$defs",
    "$ref",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "anyOf",
    "oneOf",
    "enum",
    "const",
    "discriminator",
];
/// Value constraints the generated deserializer enforces from the schema.
const VALIDATED: &[&str] = &[
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "minItems",
    "maxItems",
    "format",
];
/// Annotations: they never change which documents are valid.
const ANNOTATIONS: &[&str] = &["$schema", "title", "description", "default"];
/// Exporter extensions this crate or the schema boundary consumes.
const EXTENSIONS: &[&str] = &[
    "x-vonk-ignore-unknown",
    "x-vonk-model-bases",
    "x-vonk-model-sources",
    "x-vonk-models",
    "x-vonk-read-aliases",
    "x-vonk-source-type",
    "x-vonk-typed-tag",
];
/// Formats the validator checks (`date-time`, `uuid`, and `ip` through its
/// registered checker), and integer width hints whose bounds are
/// `minimum`/`maximum`.
const FORMATS: &[&str] = &["date-time", "uuid", "ip", "int64", "uint8"];

/// Refuse a schema that uses a keyword neither translated nor enforced.
pub(super) fn check(schema: &Value) -> Result<(), String> {
    visit(schema, "#")
}

fn visit(schema: &Value, path: &str) -> Result<(), String> {
    let Value::Object(object) = schema else {
        return Ok(());
    };
    for (key, value) in object {
        let at = format!("{path}/{key}");
        let known = [TRANSLATED, VALIDATED, ANNOTATIONS, EXTENSIONS]
            .iter()
            .any(|keys| keys.contains(&key.as_str()));
        if !known {
            return Err(format!(
                "{at}: schema keyword `{key}` is neither translated into Rust nor enforced \
                 by the generated deserializer"
            ));
        }
        if key == "format"
            && !value
                .as_str()
                .is_some_and(|format| FORMATS.contains(&format))
        {
            return Err(format!(
                "{at}: format {value} is not checked by the generated deserializer"
            ));
        }
        match key.as_str() {
            "$defs" | "properties" => {
                for (name, child) in value.as_object().into_iter().flatten() {
                    visit(child, &format!("{at}/{name}"))?;
                }
            }
            "anyOf" | "oneOf" => {
                for (index, child) in value.as_array().into_iter().flatten().enumerate() {
                    visit(child, &format!("{at}/{index}"))?;
                }
            }
            "items" | "additionalProperties" => visit(value, &at)?,
            _ => {}
        }
    }
    Ok(())
}
