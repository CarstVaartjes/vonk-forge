//! Structural validation for every generated wire deserializer.
//!
//! No field rules live here: bounds, presence, nullability, defaults and tagged
//! unions come from the checked-in export of the canonical Pydantic graph.
use serde::{
    Deserialize, Deserializer,
    de::{Error, MapAccess, Visitor},
};
use serde_json::{Value, value::RawValue};
use std::{
    collections::BTreeMap,
    sync::{Arc, LazyLock, Mutex},
};

/// Preserve original JSON kinds before schema validation. Serde's
/// arbitrary-precision Value visitor recognizes a private number-object marker;
/// a real input object must remain an object rather than acquire numeric authority.
/// RawValue also works for nested/from_value deserializers, so there is one path.
pub(crate) fn deserialize_original_value<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Value, D::Error> {
    let raw = Box::<RawValue>::deserialize(deserializer)?;
    original_value(&raw, 0).map_err(D::Error::custom)
}

pub(crate) fn deserialize_integer_number<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<serde_json::Number, D::Error> {
    match deserialize_original_value(deserializer)? {
        Value::Number(number) if !number.to_string().contains(['.', 'e', 'E']) => Ok(number),
        _ => Err(D::Error::custom("expected an integer token")),
    }
}

fn original_value(raw: &RawValue, depth: usize) -> Result<Value, String> {
    // Retain serde_json's existing nesting resource limit independently of
    // mathematical integer width. Each raw subtree still uses its JSON parser.
    if depth >= 128 {
        return Err("JSON recursion limit exceeded".to_owned());
    }
    match raw.get().trim_start().as_bytes().first() {
        Some(b'{') => {
            let mut deserializer = serde_json::Deserializer::from_str(raw.get());
            Deserializer::deserialize_map(&mut deserializer, OriginalObject { depth })
                .map_err(|error| error.to_string())
        }
        Some(b'[') => {
            let members: Vec<Box<RawValue>> =
                serde_json::from_str(raw.get()).map_err(|error| error.to_string())?;
            members
                .iter()
                .map(|member| original_value(member, depth + 1))
                .collect::<Result<Vec<_>, _>>()
                .map(Value::Array)
        }
        // Only actual scalar tokens reach this visitor. Arbitrary-precision
        // numeric lexemes remain exact; an input map cannot imitate its marker.
        _ => serde_json::from_str(raw.get()).map_err(|error| error.to_string()),
    }
}

struct OriginalObject {
    depth: usize,
}

impl<'de> Visitor<'de> for OriginalObject {
    type Value = Value;

    fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("a JSON object")
    }

    fn visit_map<M: MapAccess<'de>>(self, mut members: M) -> Result<Value, M::Error> {
        let mut object = serde_json::Map::new();
        while let Some((key, raw)) = members.next_entry::<String, Box<RawValue>>()? {
            let value = original_value(&raw, self.depth + 1).map_err(M::Error::custom)?;
            object.insert(key, value);
        }
        Ok(Value::Object(object))
    }
}

static SCHEMA: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../schema/wire.json"))
        .expect("generated wire schema must be valid JSON")
});
const SCHEMA_URI: &str = "urn:vonk:agent-wire";
static REGISTRY: LazyLock<jsonschema::Registry<'static>> = LazyLock::new(|| {
    jsonschema::Registry::new()
        .add(SCHEMA_URI, SCHEMA.clone())
        .expect("generated schema resource must be valid")
        .prepare()
        .expect("generated schema references must resolve locally")
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
    let schema = serde_json::from_str(&format!(
        r#"{{"$schema":"https://json-schema.org/draft/2020-12/schema","$ref":"{SCHEMA_URI}{pointer}"}}"#
    ))
    .map_err(|_| "generated wire schema reference is invalid")?;
    let validator = Arc::new(
        jsonschema::options()
            .with_registry(&REGISTRY)
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
    // Exact schema validation already checked nonnumeric scalar leaves. Only
    // numbers and containers can contain the strict numeric token distinctions
    // handled here; avoid compiling nullable string/bool branch validators.
    if !value.is_number() && !value.is_array() && !value.is_object() {
        return Ok(());
    }
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
    if schema.get("items").is_some()
        && let Some(array) = value.as_array()
    {
        for child in array {
            strict_numbers(&format!("{pointer}/items"), child)?;
        }
    }
    Ok(())
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum SchemaTransform {
    ReadAliases,
    Defaults,
}

// Traverse the canonical schema once per transformation phase. Aliases are
// adopted before validation; defaults only after validation. Neither phase
// invents field rules or modifies external passthrough content.
fn transform(schema: &Value, value: &mut Value, phase: SchemaTransform) {
    if let Some(reference) = schema.get("$ref").and_then(Value::as_str) {
        if let Some(schema) = reference
            .strip_prefix('#')
            .and_then(|pointer| SCHEMA.pointer(pointer))
        {
            transform(schema, value, phase);
        }
        return;
    }
    if phase == SchemaTransform::ReadAliases
        && let Some(old) = value.as_str()
        && let Some(current) = schema["x-vonk-read-aliases"].get(old)
    {
        *value = current.clone();
    }
    if let (Some(properties), Some(object)) = (
        schema.get("properties").and_then(Value::as_object),
        value.as_object_mut(),
    ) {
        for (name, property) in properties {
            if phase == SchemaTransform::Defaults
                && !object.contains_key(name)
                && let Some(default) = property.get("default")
            {
                object.insert(name.clone(), default.clone());
            }
            if let Some(child) = object.get_mut(name) {
                transform(property, child, phase);
            }
        }
    }
    if let (Some(items), Some(array)) = (schema.get("items"), value.as_array_mut()) {
        for child in array {
            transform(items, child, phase);
        }
    }
    // Defaults in union members remain the responsibility of their generated
    // nested deserializers, after the matching member has been validated.
    if phase == SchemaTransform::ReadAliases {
        for key in ["anyOf", "oneOf", "allOf"] {
            if let Some(variants) = schema[key].as_array() {
                for variant in variants {
                    transform(variant, value, phase);
                }
            }
        }
    }
}

pub(crate) fn validate_and_materialize(name: &str, value: &mut Value) -> Result<(), String> {
    transform(&SCHEMA["$defs"][name], value, SchemaTransform::ReadAliases);
    let pointer = format!("#/$defs/{name}");
    // A model declared tolerant (`extra="ignore"`) drops keys it does not know
    // before validation, so a record written by another release stays readable.
    if SCHEMA["$defs"][name]["x-vonk-ignore-unknown"] == true
        && let (Some(properties), Some(object)) = (
            SCHEMA["$defs"][name]["properties"].as_object(),
            value.as_object_mut(),
        )
    {
        object.retain(|key, _| properties.contains_key(key));
    }
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
    transform(&SCHEMA["$defs"][name], value, SchemaTransform::Defaults);
    Ok(())
}

#[cfg(test)]
mod raw_integer_shape_tests {
    use crate::generated::FailureLogTail;

    #[test]
    fn generated_tail_rejects_private_number_objects_at_raw_json_boundary() {
        let raw = br#"{"text":"tail","truncated":false,"dropped_bytes":{"$serde_json::private::Number":"2"},"dropped_lines":null}"#;
        assert!(crate::parse_strict::<FailureLogTail>(raw).is_err());
    }

    #[test]
    fn scalar_integer_rejects_original_object_and_preserves_wide_number() {
        let spoof = br#"{"$serde_json::private::Number":"18446744073709551616"}"#;
        assert!(crate::parse_strict::<crate::integer::Integer>(spoof).is_err());
        let wide = "9".repeat(200);
        let value = crate::parse_strict::<crate::integer::Integer>(wide.as_bytes()).unwrap();
        assert_eq!(value.to_string(), wide);
        for invalid in ["1.0", "1e0", "true", "null", "\"2\""] {
            assert!(crate::parse_strict::<crate::integer::Integer>(invalid.as_bytes()).is_err());
        }
    }

    #[test]
    fn original_kind_is_preserved_for_nested_and_value_deserializers() {
        // The marker is a legitimate object key when an object is expected;
        // do not replace shape preservation with a global key blacklist.
        let raw = r#"{"nested":[{"$serde_json::private::Number":"18446744073709551616"}]}"#;
        let mut decoder = serde_json::Deserializer::from_str(raw);
        let value = super::deserialize_original_value(&mut decoder).unwrap();
        assert!(value["nested"][0].is_object());
        assert_eq!(
            value["nested"][0]["$serde_json::private::Number"].as_str(),
            Some("18446744073709551616")
        );
        let through_value = super::deserialize_original_value(value.clone()).unwrap();
        assert_eq!(through_value, value);

        let raw = r#"{"text":"tail","truncated":false,"dropped_bytes":18446744073709551615,"dropped_lines":null}"#;
        let tail = crate::parse_strict::<FailureLogTail>(raw.as_bytes()).unwrap();
        assert_eq!(tail.dropped_bytes, Some(u64::MAX));
        assert_eq!(
            crate::passthrough::revalidate::<FailureLogTail>(&tail).unwrap(),
            tail
        );
    }
}
