use quote::{ToTokens, format_ident, quote};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs};
use syn::{Item, parse_quote};

use crate::declarations::{
    derive_trivial_defaults, enum_string_impl, equality_types, is_copy_type, strip_docs,
};
use crate::deserialize::{deserialize_impl, untagged_deserialize_impl};
use crate::schema::{exclusive_empty_unions, prepare, typed_tags};

/// The formatted Rust wire types for one exported Pydantic wire schema.
pub fn render(schema_path: &str) -> Result<String, Box<dyn std::error::Error>> {
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
                &names,
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
