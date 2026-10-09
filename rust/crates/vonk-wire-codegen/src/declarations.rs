use quote::{ToTokens, quote};
use std::collections::BTreeMap;
use syn::{Item, parse_quote};

pub(super) fn strip_docs(item: &mut Item) {
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
pub(super) fn is_copy_type(ty: &syn::Type) -> bool {
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
pub(super) fn derive_trivial_defaults(items: &mut Vec<Item>) {
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

pub(super) fn equality_types(items: &[Item]) -> std::collections::BTreeSet<String> {
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

pub(super) fn enum_string_impl(item: &syn::ItemEnum) -> Option<Vec<Item>> {
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
        parse_quote! { impl #ident { pub const fn as_str(&self) -> &'static str { match self { #(#arms),* } } } },
        parse_quote! { impl ::std::ops::Deref for #ident { type Target = str; fn deref(&self) -> &str { self.as_str() } } },
        parse_quote! { impl ::std::cmp::PartialEq<str> for #ident { fn eq(&self, other: &str) -> bool { self.as_str() == other } } },
        parse_quote! { impl ::std::cmp::PartialEq<&str> for #ident { fn eq(&self, other: &&str) -> bool { self.as_str() == *other } } },
    ])
}
