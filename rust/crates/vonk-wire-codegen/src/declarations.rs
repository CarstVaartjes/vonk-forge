use quote::{ToTokens, quote};
use std::collections::BTreeMap;
use syn::{Item, parse_quote};

#[expect(
    clippy::wildcard_enum_match_arm,
    reason = "syn::Item and syn::Type are non_exhaustive: a new syntax kind is not the generator's concern"
)]
pub(super) fn strip_docs(item: &mut Item) {
    let attrs = match item {
        Item::Struct(item) => &mut item.attrs,
        Item::Enum(item) => &mut item.attrs,
        _ => return,
    };
    attrs.retain(|attr| !attr.path().is_ident("doc"));
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

#[expect(
    clippy::wildcard_enum_match_arm,
    reason = "syn::Item and syn::Type are non_exhaustive: a new syntax kind is not the generator's concern"
)]
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

/// Rewrites typify's `self::...` paths to the bare path, which resolves identically
/// from the generated module and keep `unused_qualifications` quiet.
pub(super) struct SelfPaths;

impl syn::visit_mut::VisitMut for SelfPaths {
    fn visit_path_mut(&mut self, path: &mut syn::Path) {
        if path.leading_colon.is_none()
            && path.segments.len() == 3
            && path.segments[0].ident == "crate"
            && path.segments[1].ident == "integer"
        {
            path.segments = path.segments.iter().skip(2).cloned().collect();
        }
        if path.leading_colon.is_none()
            && path.segments.len() > 1
            && path.segments[0].ident == "self"
        {
            path.segments = path.segments.iter().skip(1).cloned().collect();
        }
        syn::visit_mut::visit_path_mut(self, path);
    }
}
