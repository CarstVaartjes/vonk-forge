//! Pydantic owns structure; typify owns Rust declarations. The adapter only
//! chooses scalar representations and installs exact schema validation.
use quote::{format_ident, quote};
use serde_json::{Value, json};
use std::{collections::BTreeMap, env, fs};
use syn::{Item, parse_quote};

fn prepare(value: &mut Value) {
    match value {
        Value::Object(object) => {
            if let Some(format) = object.get("format").cloned() {
                if let Some(Value::Array(variants)) = object.get_mut("anyOf") {
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
                let format = if object.get("format").and_then(Value::as_str) == Some("int64") {
                    "int64"
                } else if object
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

fn scalar_conversions(schema: &Value, settings: &mut typify::TypeSpaceSettings) {
    match schema {
        Value::Object(object) => {
            if let Some(variants) = object.get("anyOf").and_then(Value::as_array) {
                let types: Vec<_> = variants
                    .iter()
                    .filter_map(|schema| schema.get("type").and_then(Value::as_str))
                    .collect();
                if types.len() == variants.len()
                    && types.iter().filter(|kind| **kind != "null").count() > 1
                    && types.iter().all(|kind| {
                        ["integer", "number", "boolean", "string", "null"].contains(kind)
                    })
                {
                    settings.with_conversion(
                        serde_json::from_value(schema.clone()).unwrap(),
                        "::serde_json::Value",
                        [].into_iter(),
                    );
                }
            }
            for child in object.values() {
                scalar_conversions(child, settings);
            }
        }
        Value::Array(values) => {
            for value in values {
                scalar_conversions(value, settings);
            }
        }
        _ => {}
    }
}

fn strip_docs(item: &mut Item) {
    let attrs = match item {
        Item::Struct(item) => &mut item.attrs,
        Item::Enum(item) => &mut item.attrs,
        _ => return,
    };
    attrs.retain(|attr| !attr.path().is_ident("doc"));
}

fn equality_types(items: &[Item]) -> std::collections::BTreeSet<String> {
    let fields: BTreeMap<String, std::collections::BTreeSet<String>> = items
        .iter()
        .filter_map(|item| {
            let (name, tokens) = match item {
                Item::Struct(item) => {
                    let fields = &item.fields;
                    (item.ident.to_string(), quote!(#fields).to_string())
                }
                Item::Enum(item) => {
                    let variants = &item.variants;
                    (item.ident.to_string(), quote!(#variants).to_string())
                }
                _ => return None,
            };
            Some((
                name,
                tokens
                    .split(|c: char| !c.is_alphanumeric() && c != '_')
                    .filter(|v| !v.is_empty())
                    .map(str::to_owned)
                    .collect(),
            ))
        })
        .collect();
    let mut eligible: std::collections::BTreeSet<String> = fields
        .iter()
        .filter(|(_, tokens)| !tokens.contains("f64") && !tokens.contains("f32"))
        .map(|(name, _)| name.clone())
        .collect();
    loop {
        let remove: Vec<_> = eligible
            .iter()
            .filter(|name| {
                fields[*name]
                    .iter()
                    .any(|token| fields.contains_key(token) && !eligible.contains(token))
            })
            .cloned()
            .collect();
        if remove.is_empty() {
            break;
        }
        for name in remove {
            eligible.remove(&name);
        }
    }
    eligible
}

fn deserialize_impl(item: &mut Item, schema_name: &str) -> Option<Item> {
    let (ident, fields, attrs, raw, construction) = match item {
        Item::Struct(item) if matches!(item.fields, syn::Fields::Named(_)) => {
            let mut raw = item.clone();
            raw.ident = format_ident!("Raw");
            raw.vis = syn::Visibility::Inherited;
            let names: Vec<_> = item
                .fields
                .iter()
                .map(|f| f.ident.as_ref().unwrap())
                .collect();
            let construction = quote! { Self { #(#names: raw.#names),* } };
            (
                item.ident.clone(),
                true,
                &mut item.attrs,
                quote!(#raw),
                construction,
            )
        }
        Item::Enum(item) => {
            let mut raw = item.clone();
            raw.ident = format_ident!("Raw");
            raw.vis = syn::Visibility::Inherited;
            let arms = item.variants.iter().map(|variant| {
                let name = &variant.ident;
                match &variant.fields {
                    syn::Fields::Unit => quote! { Raw::#name => Self::#name },
                    syn::Fields::Unnamed(fields) => {
                        let names: Vec<_> = (0..fields.unnamed.len())
                            .map(|i| format_ident!("value{i}"))
                            .collect();
                        quote! { Raw::#name(#(#names),*) => Self::#name(#(#names),*) }
                    }
                    syn::Fields::Named(fields) => {
                        let names: Vec<_> = fields
                            .named
                            .iter()
                            .map(|f| f.ident.as_ref().unwrap())
                            .collect();
                        quote! { Raw::#name { #(#names),* } => Self::#name { #(#names),* } }
                    }
                }
            });
            let construction = quote! { match raw { #(#arms),* } };
            (
                item.ident.clone(),
                false,
                &mut item.attrs,
                quote!(#raw),
                construction,
            )
        }
        _ => return None,
    };
    let _ = fields;
    for attr in attrs
        .iter_mut()
        .filter(|attr| attr.path().is_ident("derive"))
    {
        let paths = attr
            .parse_args_with(
                syn::punctuated::Punctuated::<syn::Path, syn::Token![,]>::parse_terminated,
            )
            .unwrap();
        let retained: Vec<_> = paths
            .into_iter()
            .filter(|p| p.segments.last().unwrap().ident != "Deserialize")
            .collect();
        *attr = parse_quote!(#[derive(#(#retained),*)]);
    }
    Some(parse_quote! {
        impl<'de> ::serde::Deserialize<'de> for #ident {
            fn deserialize<D: ::serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                let mut value = <::serde_json::Value as ::serde::Deserialize>::deserialize(deserializer)?;
                crate::wire_schema::validate_and_materialize(#schema_name, &mut value)
                    .map_err(::serde::de::Error::custom)?;
                #raw
                let raw: Raw = ::serde_json::from_value(value).map_err(::serde::de::Error::custom)?;
                Ok(#construction)
            }
        }
    })
}

fn enum_string_impl(item: &syn::ItemEnum) -> Option<Vec<Item>> {
    if !item
        .variants
        .iter()
        .all(|variant| matches!(variant.fields, syn::Fields::Unit))
    {
        return None;
    }
    let ident = &item.ident;
    let mut arms = Vec::new();
    for variant in &item.variants {
        let name = &variant.ident;
        let mut text = name.to_string();
        for attr in &variant.attrs {
            if attr.path().is_ident("serde") {
                attr.parse_nested_meta(|meta| {
                    if meta.path.is_ident("rename") {
                        text = meta.value()?.parse::<syn::LitStr>()?.value();
                    }
                    Ok(())
                })
                .unwrap();
            }
        }
        arms.push(quote! { Self::#name => #text });
    }
    Some(vec![
        parse_quote! { impl #ident { pub fn as_str(&self) -> &'static str { match self { #(#arms),* } } } },
        parse_quote! { impl ::std::ops::Deref for #ident { type Target = str; fn deref(&self) -> &str { self.as_str() } } },
        parse_quote! { impl ::std::cmp::PartialEq<str> for #ident { fn eq(&self, other: &str) -> bool { self.as_str() == other } } },
        parse_quote! { impl ::std::cmp::PartialEq<&str> for #ident { fn eq(&self, other: &&str) -> bool { self.as_str() == *other } } },
    ])
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args().skip(1);
    let schema_path = args.next().ok_or("schema path is required")?;
    let output_path = args.next().ok_or("output path is required")?;
    let check = args.next().as_deref() == Some("--check");
    let mut schema: Value = serde_json::from_slice(&fs::read(schema_path)?)?;
    prepare(&mut schema);
    let defs = schema.get_mut("$defs").ok_or("missing $defs")?.take();
    let defs_text = serde_json::to_string(&defs)?.replace("#/$defs/", "#/definitions/");
    let values: BTreeMap<String, Value> = serde_json::from_str(&defs_text)?;
    let names: BTreeMap<String, String> = values
        .keys()
        .map(|name| (name.trim_start_matches('_').to_owned(), name.clone()))
        .collect();
    let definitions: BTreeMap<String, schemars::schema::Schema> = values
        .into_iter()
        .map(|(name, value)| {
            serde_json::from_value(value)
                .map(|schema| (name.clone(), schema))
                .map_err(|error| format!("{name}: {error}"))
        })
        .collect::<Result<_, _>>()?;
    let mut settings = typify::TypeSpaceSettings::default();
    settings.with_derive("PartialEq".into());
    settings.with_map_type("::std::collections::BTreeMap");
    scalar_conversions(&serde_json::from_str::<Value>(&defs_text)?, &mut settings);
    settings.with_conversion(
        serde_json::from_value(json!({"type":"string", "format":"date-time"}))?,
        "::chrono::DateTime<::chrono::FixedOffset>",
        [].into_iter(),
    );
    settings.with_conversion(
        serde_json::from_value(json!({"type":"string","format":"ip"}))?,
        "::std::net::IpAddr",
        [].into_iter(),
    );
    for name in ["JsonValue", "RuntimeArgumentValue"] {
        settings.with_replacement(name, "::serde_json::Value", [].into_iter());
    }
    let mut types = typify::TypeSpace::new(&settings);
    types.add_ref_types(definitions)?;
    let mut syntax: syn::File = syn::parse2(types.to_stream())?;
    let eq_types = equality_types(&syntax.items);
    let mut validation = Vec::new();
    for item in &mut syntax.items {
        strip_docs(item);
        if let Item::Enum(item) = item {
            if let Some(implementations) = enum_string_impl(item) {
                validation.extend(implementations);
            }
        }
        let name = match item {
            Item::Struct(item) => item.ident.to_string(),
            Item::Enum(item) => item.ident.to_string(),
            _ => continue,
        };
        if eq_types.contains(&name) {
            let attrs = match item {
                Item::Struct(item) => &mut item.attrs,
                Item::Enum(item) => &mut item.attrs,
                _ => unreachable!(),
            };
            let already = attrs
                .iter()
                .filter(|attr| attr.path().is_ident("derive"))
                .any(|attr| {
                    attr.parse_args_with(
                        syn::punctuated::Punctuated::<syn::Path, syn::Token![,]>::parse_terminated,
                    )
                    .unwrap()
                    .iter()
                    .any(|path| path.is_ident("Eq"))
                });
            if !already {
                attrs.push(parse_quote!(#[derive(Eq)]));
            }
        }
        if let Some(schema_name) = names.get(&name) {
            if let Some(implementation) = deserialize_impl(item, schema_name) {
                validation.push(implementation);
            }
        }
    }
    syntax.items.extend(validation);
    let content = format!(
        "// @generated by scripts/generate-agent-wire. Do not edit.\n{}",
        prettyplease::unparse(&syntax)
    );
    use std::io::Write;
    let mut formatter = std::process::Command::new("rustfmt")
        .args(["--edition", "2024", "--emit", "stdout"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()?;
    formatter
        .stdin
        .take()
        .unwrap()
        .write_all(content.as_bytes())?;
    let formatted = formatter.wait_with_output()?;
    if !formatted.status.success() {
        return Err("pinned rustfmt failed".into());
    }
    let content = String::from_utf8(formatted.stdout)?;
    if check {
        if fs::read_to_string(&output_path)? != content {
            return Err(format!("stale generated Rust wire types: {output_path}").into());
        }
    } else {
        fs::write(output_path, content)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
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
}
