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

fn validator(name: &str) -> Result<Arc<jsonschema::Validator>, String> {
    let mut validators = VALIDATORS
        .lock()
        .map_err(|_| "wire schema cache unavailable")?;
    if let Some(validator) = validators.get(name) {
        return Ok(validator.clone());
    }
    if SCHEMA["$defs"].get(name).is_none() {
        return Err("unknown generated wire model".into());
    }
    let schema = json!({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": SCHEMA["$defs"],
        "$ref": format!("#/$defs/{name}"),
    });
    let validator = Arc::new(
        jsonschema::options()
            .should_validate_formats(true)
            .build(&schema)
            .map_err(|_| "generated wire schema cannot be compiled")?,
    );
    validators.insert(name.to_owned(), validator.clone());
    Ok(validator)
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
    let validator = validator(name)?;
    if let Err(error) = validator.validate(&*value) {
        // Only the model and structural path are exposed, never the instance
        // value (which may include credentials or signed authority material).
        return Err(format!(
            "invalid {name} wire document at {}",
            error.instance_path()
        ));
    }
    materialize(&SCHEMA["$defs"][name], value);
    Ok(())
}
