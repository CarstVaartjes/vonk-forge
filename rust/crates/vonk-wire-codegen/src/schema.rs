use serde_json::{Value, json};

pub(super) fn prepare(value: &mut Value) {
    match value {
        Value::Object(object) => {
            if object.get("x-vonk-source-type").and_then(Value::as_str) == Some("string") {
                object.remove("format");
            }
            if let Some(format) = object.get("format").cloned()
                && let Some(Value::Array(variants)) = object.get_mut("anyOf")
            {
                for variant in variants {
                    if variant.get("type").and_then(Value::as_str) == Some("string") {
                        variant
                            .as_object_mut()
                            .unwrap()
                            .insert("format".into(), format.clone());
                    }
                }
                object.remove("format");
            }
            let uuid = object.get("format").and_then(Value::as_str) == Some("uuid")
                || object
                    .get("pattern")
                    .and_then(Value::as_str)
                    .is_some_and(|pattern| pattern.contains("[0-9a-f]{8}-[0-9a-f]{4}"));
            let timestamp = object.get("format").and_then(Value::as_str) == Some("date-time");
            let ip = object.get("format").and_then(Value::as_str) == Some("ip");
            // Materialize non-null canonical defaults before the generated Raw
            // deserializer. They remain optional in the authoritative schema.
            let defaults: Vec<_> = object
                .get("properties")
                .and_then(Value::as_object)
                .into_iter()
                .flat_map(|properties| properties.iter())
                .filter(|(_, schema)| schema.get("default").is_some_and(|v| !v.is_null()))
                .map(|(name, _)| Value::String(name.clone()))
                .collect();
            if !defaults.is_empty() {
                let required = object
                    .entry("required")
                    .or_insert_with(|| json!([]))
                    .as_array_mut()
                    .unwrap();
                for name in defaults {
                    if !required.contains(&name) {
                        required.push(name);
                    }
                }
            }
            // typify 0.7 does not correctly process all Pydantic defaults. Exact
            // defaults are applied by generated Deserialize, never discarded.
            object.remove("default");
            object.remove("title");
            object.remove("description");
            object.remove("not");
            for key in ["pattern", "minLength", "maxLength"] {
                object.remove(key);
            }
            if object.get("type").and_then(Value::as_str) == Some("string") {
                for key in ["pattern", "minLength", "maxLength", "format"] {
                    object.remove(key);
                }
                if ip {
                    object.insert("format".into(), json!("ip"));
                }
                if uuid {
                    object.insert("format".into(), json!("uuid"));
                }
                if timestamp {
                    object.insert("format".into(), json!("date-time"));
                }
            }
            if object.get("type").and_then(Value::as_str) == Some("integer") {
                let unsigned = object
                    .get("minimum")
                    .and_then(Value::as_f64)
                    .is_some_and(|v| v >= 0.0)
                    || object
                        .get("exclusiveMinimum")
                        .and_then(Value::as_f64)
                        .is_some_and(|v| v >= 0.0)
                    || object.get("const").and_then(Value::as_u64).is_some();
                let unbounded = !["maximum", "exclusiveMaximum", "const", "enum"]
                    .iter()
                    .any(|key| object.contains_key(*key));
                let format = if unbounded {
                    "vonk-integer"
                } else if object.get("format").and_then(Value::as_str) == Some("int64") {
                    "int64"
                } else if (object.get("format").and_then(Value::as_str) == Some("uint8")
                    && object.get("minimum").and_then(Value::as_u64).is_some()
                    && object
                        .get("maximum")
                        .and_then(Value::as_u64)
                        .is_some_and(|maximum| maximum <= u8::MAX as u64))
                    || object
                        .get("const")
                        .and_then(Value::as_u64)
                        .is_some_and(|v| v <= 255)
                {
                    "uint8"
                } else if unsigned && object.get("maximum").and_then(Value::as_u64) == Some(65535) {
                    "uint16"
                } else if unsigned
                    && object
                        .get("maximum")
                        .and_then(Value::as_u64)
                        .is_some_and(|maximum| maximum <= u32::MAX as u64)
                {
                    "uint32"
                } else if unsigned {
                    "uint64"
                } else {
                    "int64"
                };
                object.insert("format".into(), Value::String(format.into()));
                for key in [
                    "minimum",
                    "maximum",
                    "exclusiveMinimum",
                    "exclusiveMaximum",
                    "multipleOf",
                ] {
                    object.remove(key);
                }
            }
            // These constraints are enforced from the exact exported schema,
            // rather than duplicated in ergonomic scalar wrapper types.
            if object.get("type").and_then(Value::as_str) == Some("number") {
                for key in [
                    "minimum",
                    "maximum",
                    "exclusiveMinimum",
                    "exclusiveMaximum",
                    "multipleOf",
                ] {
                    object.remove(key);
                }
            }
            for key in [
                "$defs",
                "definitions",
                "properties",
                "patternProperties",
                "dependentSchemas",
            ] {
                if let Some(Value::Object(children)) = object.get_mut(key) {
                    for child in children.values_mut() {
                        prepare(child);
                    }
                }
            }
            // A union of named models is a closed choice. typify only emits an
            // enum for anyOf when it can prove the variants exclusive, which an
            // empty success model (`{}`) defeats; oneOf keeps the enum.
            if let Some(Value::Array(variants)) = object.get("anyOf")
                && !variants.is_empty()
                && variants.iter().all(|variant| {
                    variant
                        .as_object()
                        .is_some_and(|variant| variant.len() == 1 && variant.contains_key("$ref"))
                })
                && !object.contains_key("oneOf")
            {
                let variants = object.remove("anyOf").unwrap();
                object.insert("oneOf".into(), variants);
            }
            for key in ["anyOf", "oneOf", "allOf", "prefixItems"] {
                if let Some(Value::Array(children)) = object.get_mut(key) {
                    for child in children {
                        prepare(child);
                    }
                }
            }
            for key in [
                "items",
                "additionalProperties",
                "propertyNames",
                "contains",
                "if",
                "then",
                "else",
            ] {
                if let Some(child) = object.get_mut(key) {
                    prepare(child);
                }
            }
        }
        Value::Array(array) => {
            for child in array {
                prepare(child);
            }
        }
        _ => {}
    }
}

/// Give a marked union tag a typed, single-variant enum.
///
/// Pydantic describes the tag of a union member as a string `const`, which
/// typify declares as a free `String`: a producer could then build a member
/// whose tag disagrees with its shape. A tag marked `x-vonk-typed-tag` becomes a
/// one-variant enum instead, so the generated constructor cannot say anything
/// else. Tags that are not marked keep their `String` declaration.
pub(super) fn typed_tags(value: &mut Value) {
    match value {
        Value::Object(object) => {
            if object.get("x-vonk-typed-tag") == Some(&Value::Bool(true))
                && let Some(tag) = object.get("const").cloned()
            {
                object.remove("const");
                object.remove("x-vonk-typed-tag");
                object.insert("type".into(), json!("string"));
                object.insert("enum".into(), json!([tag]));
            }
            for child in object.values_mut() {
                typed_tags(child);
            }
        }
        Value::Array(values) => {
            for child in values {
                typed_tags(child);
            }
        }
        _ => {}
    }
}

/// Declare a union of model references exclusive when one variant is an empty
/// message. typify cannot prove `{}` disjoint from the other variants and would
/// otherwise flatten the union into a struct of optional parts. Only the Rust
/// declarations change: the authoritative schema, which validates every
/// document, keeps its `anyOf`, and callers match the empty result by content.
pub(super) fn exclusive_empty_unions(schema: &mut Value) {
    let empty: Vec<String> = schema
        .get("$defs")
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
        .filter(|(_, definition)| {
            definition.get("type").and_then(Value::as_str) == Some("object")
                && definition
                    .get("properties")
                    .and_then(Value::as_object)
                    .is_some_and(|properties| properties.is_empty())
        })
        .map(|(name, _)| format!("#/$defs/{name}"))
        .collect();
    fn visit(value: &mut Value, empty: &[String]) {
        match value {
            Value::Object(object) => {
                let rewrite =
                    object
                        .get("anyOf")
                        .and_then(Value::as_array)
                        .is_some_and(|variants| {
                            variants.iter().all(|variant| variant.get("$ref").is_some())
                                && variants.iter().any(|variant| {
                                    variant.get("$ref").and_then(Value::as_str).is_some_and(
                                        |reference| empty.iter().any(|e| e == reference),
                                    )
                                })
                        });
                if rewrite && let Some(variants) = object.remove("anyOf") {
                    object.insert("oneOf".into(), variants);
                }
                for child in object.values_mut() {
                    visit(child, empty);
                }
            }
            Value::Array(values) => {
                for child in values {
                    visit(child, empty);
                }
            }
            _ => {}
        }
    }
    visit(schema, &empty);
}
