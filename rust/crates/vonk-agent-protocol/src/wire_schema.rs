//! Structural validation for every generated wire deserializer.
//!
//! No field rules live here: bounds, presence, nullability, defaults and tagged
//! unions come from the checked-in export of the canonical Pydantic graph.
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{Arc, LazyLock, Mutex},
};

static SCHEMA: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../schema/wire.json"))
        .expect("generated wire schema must be valid JSON")
});
static VALIDATORS: LazyLock<Mutex<BTreeMap<String, Arc<jsonschema::Validator>>>> =
    LazyLock::new(|| Mutex::new(BTreeMap::new()));

fn validator(pointer: &str) -> Result<Arc<jsonschema::Validator>, String> {
    let mut validators = VALIDATORS
        .lock()
        .map_err(|_| "wire schema cache unavailable")?;
    if let Some(validator) = validators.get(pointer) {
        return Ok(validator.clone());
    }
    if SCHEMA.pointer(pointer.trim_start_matches('#')).is_none() {
        return Err("unknown generated wire model".into());
    }
    let schema = json!({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": SCHEMA["$defs"],
        "$ref": pointer,
    });
    let validator = Arc::new(
        jsonschema::options()
            .should_validate_formats(true)
            .with_format("ip", |value: &str| {
                value
                    .parse::<std::net::IpAddr>()
                    .is_ok_and(|address| address.to_string() == value)
            })
            .build(&schema)
            .map_err(|_| "generated wire schema cannot be compiled")?,
    );
    validators.insert(pointer.to_owned(), validator.clone());
    Ok(validator)
}

fn pointer_component(value: &str) -> String {
    value.replace('~', "~0").replace('/', "~1")
}

fn strict_numbers(pointer: &str, value: &Value) -> Result<(), String> {
    let schema = SCHEMA
        .pointer(pointer.trim_start_matches('#'))
        .ok_or("unknown wire schema path")?;
    if let Some(reference) = schema.get("$ref").and_then(Value::as_str) {
        return strict_numbers(reference, value);
    }
    for keyword in ["anyOf", "oneOf"] {
        if let Some(variants) = schema.get(keyword).and_then(Value::as_array) {
            // JSON Schema treats an integral floating token as an integer and
            // lets out-of-range integer tokens fall through to a number union.
            // Pydantic strict scalar unions distinguish those wire values.
            let integer_token = value
                .as_number()
                .is_some_and(|number| !number.to_string().contains(['.', 'e', 'E']));
            let integer_branches: Vec<_> = variants
                .iter()
                .enumerate()
                .filter(|(_, schema)| schema.get("type").and_then(Value::as_str) == Some("integer"))
                .map(|(index, _)| index)
                .collect();
            if integer_token && !integer_branches.is_empty() {
                for index in integer_branches {
                    if validator(&format!("{pointer}/{keyword}/{index}"))?.is_valid(value) {
                        return Ok(());
                    }
                }
                return Err("integer wire value is outside its declared range".into());
            }
            for (index, _) in variants.iter().enumerate() {
                let branch = format!("{pointer}/{keyword}/{index}");
                if validator(&branch)?.is_valid(value) && strict_numbers(&branch, value).is_ok() {
                    return Ok(());
                }
            }
            return Err("wire scalar does not match its declared union".into());
        }
    }
    if schema.get("type").and_then(Value::as_str) == Some("number")
        && value.is_number()
        && !value.as_f64().is_some_and(f64::is_finite)
    {
        return Err("floating wire value must be finite".into());
    }
    if schema.get("type").and_then(Value::as_str) == Some("integer")
        && value
            .as_number()
            .is_some_and(|number| number.to_string().contains(['.', 'e', 'E']))
    {
        return Err("integer wire value must use an integer token".into());
    }
    if let (Some(properties), Some(object)) = (
        schema.get("properties").and_then(Value::as_object),
        value.as_object(),
    ) {
        for (name, child) in object {
            if properties.contains_key(name) {
                strict_numbers(
                    &format!("{pointer}/properties/{}", pointer_component(name)),
                    child,
                )?;
            } else if schema
                .get("additionalProperties")
                .is_some_and(Value::is_object)
            {
                strict_numbers(&format!("{pointer}/additionalProperties"), child)?;
            }
        }
    } else if let (Some(_), Some(object)) = (
        schema.get("additionalProperties").filter(|v| v.is_object()),
        value.as_object(),
    ) {
        for child in object.values() {
            strict_numbers(&format!("{pointer}/additionalProperties"), child)?;
        }
    }
    if schema.get("items").is_some() {
        if let Some(array) = value.as_array() {
            for child in array {
                strict_numbers(&format!("{pointer}/items"), child)?;
            }
        }
    }
    Ok(())
}

fn materialize(schema: &Value, value: &mut Value) {
    if let Some(reference) = schema.get("$ref").and_then(Value::as_str) {
        if let Some(schema) = reference
            .strip_prefix('#')
            .and_then(|pointer| SCHEMA.pointer(pointer))
        {
            materialize(schema, value);
        }
        return;
    }
    // Defaults in union members are materialized by their generated nested
    // deserializers, after the matching member has been selected and validated.
    if let (Some(properties), Some(object)) = (
        schema.get("properties").and_then(Value::as_object),
        value.as_object_mut(),
    ) {
        for (name, property) in properties {
            if !object.contains_key(name) {
                if let Some(default) = property.get("default") {
                    object.insert(name.clone(), default.clone());
                }
            }
            if let Some(child) = object.get_mut(name) {
                materialize(property, child);
            }
        }
    }
    if let (Some(items), Some(array)) = (schema.get("items"), value.as_array_mut()) {
        for child in array {
            materialize(items, child);
        }
    }
}

pub(crate) fn validate_and_materialize(name: &str, value: &mut Value) -> Result<(), String> {
    let pointer = format!("#/$defs/{name}");
    let validator = validator(&pointer)?;
    if let Err(error) = validator.validate(&*value) {
        // Only the model and structural path are exposed, never the instance
        // value (which may include credentials or signed authority material).
        return Err(format!(
            "invalid {name} wire document at {}",
            error.instance_path()
        ));
    }
    strict_numbers(&pointer, value)?;
    materialize(&SCHEMA["$defs"][name], value);
    Ok(())
}
