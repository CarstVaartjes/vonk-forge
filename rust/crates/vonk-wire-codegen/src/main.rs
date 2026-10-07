//! Pydantic owns structure; typify owns Rust declarations. The adapter only
//! chooses scalar representations and installs exact schema validation.
use quote::{ToTokens, format_ident, quote};
use serde_json::{Value, json};
use std::{collections::BTreeMap, env, fs};
use syn::{Item, parse_quote};

fn prepare(value: &mut Value) {
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
                } else if object.get("format").and_then(Value::as_str) == Some("uint8")
                    && object.get("minimum").and_then(Value::as_u64).is_some()
                    && object
                        .get("maximum")
                        .and_then(Value::as_u64)
                        .is_some_and(|maximum| maximum <= u8::MAX as u64)
                {
                    "uint8"
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

fn strip_docs(item: &mut Item) {
    let attrs = match item {
        Item::Struct(item) => &mut item.attrs,
        Item::Enum(item) => &mut item.attrs,
        _ => return,
    };
    attrs.retain(|attr| !attr.path().is_ident("doc"));
    // typify's union variants mirror canonical model names and use inline
    // payloads. Keep that generated API stable; names and layout are not
    // handwritten choices. Copy this narrowly scoped attribute to Raw too.
    if let Item::Enum(item) = item
        && item
            .variants
            .iter()
            .any(|variant| !matches!(variant.fields, syn::Fields::Unit))
    {
        item.attrs
            .push(parse_quote!(#[allow(clippy::large_enum_variant, clippy::enum_variant_names)]));
    }
    // A closed word set whose members all live in one namespace (`run.*`,
    // `resource.*`) names every variant with that namespace as its prefix.
    // The wire words, not the Rust names, are the contract.
    if let Item::Enum(item) = item
        && item.variants.len() >= 3
        && item
            .variants
            .iter()
            .all(|variant| matches!(variant.fields, syn::Fields::Unit))
    {
        let prefixes: std::collections::BTreeSet<String> = item
            .variants
            .iter()
            .map(|variant| {
                let name = variant.ident.to_string();
                let tail = name.chars().skip(1).position(char::is_uppercase);
                name[..tail.map_or(name.len(), |index| index + 1)].to_string()
            })
            .collect();
        if prefixes.len() == 1 {
            item.attrs
                .push(parse_quote!(#[allow(clippy::enum_variant_names)]));
        }
    }
    if let Item::Struct(item) = item {
        for field in &mut item.fields {
            if let syn::Type::Path(path) = &field.ty {
                let leaf = path.path.segments.last().unwrap();
                if leaf.ident == "DateTime" {
                    field.attrs.push(parse_quote!(#[serde(serialize_with = "crate::wire_datetime::serialize", deserialize_with = "crate::wire_datetime::deserialize")]));
                } else if leaf.ident == "Option"
                    && field.ty.to_token_stream().to_string().contains("DateTime")
                {
                    field.attrs.push(parse_quote!(#[serde(serialize_with = "crate::wire_datetime::serialize_optional", deserialize_with = "crate::wire_datetime::deserialize_optional")]));
                }
            }
            for attr in &mut field.attrs {
                if attr.path().is_ident("serde") {
                    let options = attr.parse_args_with(syn::punctuated::Punctuated::<syn::Meta, syn::Token![,]>::parse_terminated).unwrap();
                    let kept: Vec<_> = options.into_iter().filter(|option| {
                        !matches!(option, syn::Meta::NameValue(value) if value.path.is_ident("skip_serializing_if") && value.value.to_token_stream().to_string().contains("is_empty"))
                    }).collect();
                    *attr = parse_quote!(#[serde(#(#kept),*)]);
                }
            }
        }
    }
}

// Only classify Rust types whose Copy contract is known. Unknown/generated
// types keep Clone; this never infers ownership from a wire field's name.
fn is_copy_type(ty: &syn::Type) -> bool {
    if let syn::Type::Tuple(tuple) = ty {
        return tuple.elems.iter().all(is_copy_type);
    }
    let syn::Type::Path(path) = ty else {
        return false;
    };
    let name = path.path.to_token_stream().to_string().replace(' ', "");
    if [
        "bool",
        "u8",
        "u16",
        "u32",
        "u64",
        "i8",
        "i16",
        "i32",
        "i64",
        "f32",
        "f64",
        "::uuid::Uuid",
        "::std::net::IpAddr",
        "::chrono::DateTime<::chrono::FixedOffset>",
    ]
    .contains(&name.as_str())
    {
        return true;
    }
    let leaf = path.path.segments.last().unwrap();
    if leaf.ident == "Option"
        && let syn::PathArguments::AngleBracketed(arguments) = &leaf.arguments
        && let Some(syn::GenericArgument::Type(inner)) = arguments.args.first()
    {
        return is_copy_type(inner);
    }
    false
}

// Replace only typify defaults that are exactly a derived field-by-field
// Default. Schema defaults still materialize in the validated Deserialize path.
fn derive_trivial_defaults(items: &mut Vec<Item>) {
    let mut derived = Vec::new();
    items.retain(|item| {
        let Item::Impl(item) = item else { return true };
        let Some((trait_path, _)) = &item.trait_ else {
            return true;
        };
        if trait_path.segments.last().unwrap().ident != "Default" {
            return true;
        }
        let [syn::ImplItem::Fn(method)] = item.items.as_slice() else {
            return true;
        };
        let [syn::Stmt::Expr(syn::Expr::Struct(value), None)] = method.block.stmts.as_slice()
        else {
            return true;
        };
        if !value.path.is_ident("Self")
            || value.rest.is_some()
            || !value.fields.iter().all(|field| {
                field.expr.to_token_stream().to_string() == quote!(Default::default()).to_string()
            })
        {
            return true;
        }
        derived.push(item.self_ty.to_token_stream().to_string());
        false
    });
    for item in items {
        if let Item::Struct(item) = item
            && derived.contains(&item.ident.to_string())
        {
            item.attrs.push(parse_quote!(#[derive(Default)]));
        }
    }
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

// Serde's derived untagged enum buffers Content, which cannot represent
// arbitrary-precision integers or preserve RawValue through nested models.
// Try the generated payload types directly from the original-kind Value instead.
fn untagged_deserialize_impl(item: &mut syn::ItemEnum, schema_name: Option<&str>) -> Item {
    let ident = &item.ident;
    let branches = item.variants.iter().map(|variant| {
        let name = &variant.ident;
        match &variant.fields {
            syn::Fields::Unit => quote! {
                if ::serde_json::from_value::<()>(value.clone()).is_ok() {
                    return Ok(Self::#name);
                }
            },
            syn::Fields::Unnamed(fields) if fields.unnamed.len() == 1 => {
                let payload = &fields.unnamed.first().unwrap().ty;
                quote! {
                    if let Ok(payload) = ::serde_json::from_value::<#payload>(value.clone()) {
                        return Ok(Self::#name(payload));
                    }
                }
            }
            syn::Fields::Unnamed(fields) => {
                let types: Vec<_> = fields.unnamed.iter().map(|field| &field.ty).collect();
                let names: Vec<_> = (0..types.len()).map(|index| format_ident!("value{index}")).collect();
                quote! {
                    if let Ok((#(#names),*)) = ::serde_json::from_value::<(#(#types),*)>(value.clone()) {
                        return Ok(Self::#name(#(#names),*));
                    }
                }
            }
            syn::Fields::Named(fields) => {
                let helper = format_ident!("Raw{name}");
                let names: Vec<_> = fields.named.iter().map(|field| field.ident.as_ref().unwrap()).collect();
                quote! {
                    #[derive(::serde::Deserialize)]
                    struct #helper #fields
                    if let Ok(payload) = ::serde_json::from_value::<#helper>(value.clone()) {
                        return Ok(Self::#name { #(#names: payload.#names),* });
                    }
                }
            }
        }
    });
    let validation = schema_name.map(|schema_name| {
        quote! {
            crate::wire_schema::validate_and_materialize(#schema_name, &mut value)
                .map_err(::serde::de::Error::custom)?;
        }
    });
    let implementation = parse_quote! {
        impl<'de> ::serde::Deserialize<'de> for #ident {
            fn deserialize<D: ::serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                #[allow(unused_mut)]
                let mut value = crate::wire_schema::deserialize_original_value(deserializer)?;
                #validation
                #(#branches)*
                Err(::serde::de::Error::custom(concat!("invalid canonical union ", stringify!(#ident))))
            }
        }
    };
    for attr in item
        .attrs
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
            .filter(|path| path.segments.last().unwrap().ident != "Deserialize")
            .collect();
        *attr = parse_quote!(#[derive(#(#retained),*)]);
    }
    implementation
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
                let mut value = crate::wire_schema::deserialize_original_value(deserializer)?;
                crate::wire_schema::validate_and_materialize(#schema_name, &mut value)
                    .map_err(::serde::de::Error::custom)?;
                #raw
                // An empty message constructs itself without reading `raw`.
                #[allow(unused_variables)]
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
                    } else if meta.path.is_ident("alias") {
                        let _ = meta.value()?.parse::<syn::LitStr>()?;
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
    fs::write(output_path, render(&schema_path)?)?;
    Ok(())
}

/// Give a marked union tag a typed, single-variant enum.
///
/// Pydantic describes the tag of a union member as a string `const`, which
/// typify declares as a free `String`: a producer could then build a member
/// whose tag disagrees with its shape. A tag marked `x-vonk-typed-tag` becomes a
/// one-variant enum instead, so the generated constructor cannot say anything
/// else. Tags that are not marked keep their `String` declaration.
fn typed_tags(value: &mut Value) {
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
fn exclusive_empty_unions(schema: &mut Value) {
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

/// The formatted Rust wire types for one exported Pydantic wire schema.
fn render(schema_path: &str) -> Result<String, Box<dyn std::error::Error>> {
    let mut schema: Value = serde_json::from_slice(&fs::read(schema_path)?)?;
    let bases = schema
        .get("x-vonk-model-bases")
        .cloned()
        .unwrap_or(json!({}));
    let read_aliases = schema["$defs"]
        .as_object()
        .unwrap()
        .iter()
        .filter_map(|(name, definition)| {
            definition
                .get("x-vonk-read-aliases")
                .map(|aliases| (name.clone(), aliases.clone()))
        })
        .collect::<BTreeMap<_, _>>();
    typed_tags(&mut schema);
    prepare(&mut schema);
    exclusive_empty_unions(&mut schema);
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
    // A canonical unbounded integer cannot be represented by i64/u64 or f64.
    // The owned scalar also rejects decimal/exponent lexemes, including when
    // an anonymous generated union is deserialized without its parent model.
    settings.with_conversion(
        serde_json::from_value(json!({"type":"integer","format":"vonk-integer"}))?,
        "crate::integer::Integer",
        [
            typify::TypeSpaceImpl::Display,
            typify::TypeSpaceImpl::Default,
        ]
        .into_iter(),
    );
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
    derive_trivial_defaults(&mut syntax.items);
    let eq_types = equality_types(&syntax.items);
    let mut validation = Vec::new();
    for item in &mut syntax.items {
        strip_docs(item);
        if let Item::Enum(item) = item
            && let Some(implementations) = enum_string_impl(item)
        {
            validation.extend(implementations);
        }
        let name = match item {
            Item::Struct(item) => item.ident.to_string(),
            Item::Enum(item) => item.ident.to_string(),
            _ => continue,
        };
        if let Item::Enum(enumeration) = item
            && let Some(aliases) = read_aliases.get(&name).and_then(Value::as_object)
        {
            for variant in &mut enumeration.variants {
                for (old, current) in aliases {
                    let current = current.as_str().unwrap();
                    if variant.attrs.iter().any(|attr| {
                        attr.meta
                            .to_token_stream()
                            .to_string()
                            .contains(&format!("\"{current}\""))
                    }) {
                        variant.attrs.push(parse_quote!(#[serde(alias = #old)]));
                    }
                }
            }
        }
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
        if let Item::Enum(enumeration) = item
            && enumeration.attrs.iter().any(|attr| {
                attr.path().is_ident("serde")
                    && attr.meta.to_token_stream().to_string().contains("untagged")
            })
        {
            validation.push(untagged_deserialize_impl(
                enumeration,
                names.get(&name).map(String::as_str),
            ));
        } else if let Some(schema_name) = names.get(&name)
            && let Some(implementation) = deserialize_impl(item, schema_name)
        {
            validation.push(implementation);
        }
    }
    // Pydantic inheritance defines identity projections. Generate their field
    // selection from typify declarations, rather than hand-copying wire fields.
    for (derived, base_names) in bases.as_object().unwrap() {
        let derived_ident = format_ident!("{derived}");
        for base in base_names.as_array().unwrap() {
            let base = base.as_str().unwrap();
            let base_ident = format_ident!("{base}");
            let fields = syntax
                .items
                .iter()
                .find_map(|item| match item {
                    Item::Struct(item) if item.ident == base => Some(
                        item.fields
                            .iter()
                            .map(|field| field.ident.clone().unwrap())
                            .collect::<Vec<_>>(),
                    ),
                    _ => None,
                })
                .ok_or("inherited base is not a generated struct")?;
            let derived_fields = syntax
                .items
                .iter()
                .find_map(|item| match item {
                    Item::Struct(item) if item.ident == derived => Some(&item.fields),
                    _ => None,
                })
                .ok_or("inherited model is not a generated struct")?;
            let base_fields = syntax
                .items
                .iter()
                .find_map(|item| match item {
                    Item::Struct(item) if item.ident == base => Some(&item.fields),
                    _ => None,
                })
                .unwrap();
            // Pydantic subclasses may deliberately narrow fields. A projection
            // is infallible only when typify represents every inherited field
            // identically; narrowed models retain their own semantic behavior.
            if !base_fields.iter().all(|base_field| {
                derived_fields.iter().any(|field| {
                    field.ident == base_field.ident
                        && field.ty.to_token_stream().to_string()
                            == base_field.ty.to_token_stream().to_string()
                })
            }) {
                continue;
            }
            let values: Vec<_> = base_fields
                .iter()
                .map(|field| {
                    let name = field.ident.as_ref().unwrap();
                    if is_copy_type(&field.ty) {
                        quote!(value.#name)
                    } else {
                        quote!(value.#name.clone())
                    }
                })
                .collect();
            validation.push(parse_quote! {
                impl From<&#derived_ident> for #base_ident {
                    fn from(value: &#derived_ident) -> Self {
                        Self { #(#fields: #values),* }
                    }
                }
            });
        }
    }
    syntax.items.extend(validation);
    // The canonical unbounded integer is a JSON scalar, not an independently
    // authored object contract. Generate its serde representation together with
    // the declarations which select it through the conversion above. Semantic
    // ordering and machine-integer conversions remain ordinary Rust behavior.
    let integer_ident = format_ident!("Integer");
    let integer: syn::File = parse_quote! {
        #[derive(Clone, Debug, PartialEq, Eq)]
        pub struct #integer_ident(::serde_json::Number);

        impl #integer_ident {
            pub(crate) fn number(&self) -> &::serde_json::Number {
                &self.0
            }
            pub(crate) fn from_i64(value: i64) -> Self {
                Self(value.into())
            }
            pub(crate) fn from_u64(value: u64) -> Self {
                Self(value.into())
            }
        }

        impl<'de> ::serde::Deserialize<'de> for #integer_ident {
            fn deserialize<D: ::serde::Deserializer<'de>>(
                deserializer: D,
            ) -> Result<Self, D::Error> {
                crate::wire_schema::deserialize_integer_number(deserializer).map(Self)
            }
        }

        impl ::serde::Serialize for #integer_ident {
            fn serialize<S: ::serde::Serializer>(
                &self,
                serializer: S,
            ) -> Result<S::Ok, S::Error> {
                ::serde::Serialize::serialize(&self.0, serializer)
            }
        }
    };
    syntax.items.extend(integer.items);
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
    Ok(String::from_utf8(formatted.stdout)?)
}

#[cfg(test)]
mod tests {
    use super::*;

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
        let protocol =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../vonk-agent-protocol");
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
}
