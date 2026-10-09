use quote::{format_ident, quote};
use std::collections::BTreeMap;
use syn::{Item, parse_quote};

fn named_payload_schema<'a>(
    payload: &syn::Type,
    schema_names: &'a BTreeMap<String, String>,
) -> Option<&'a String> {
    let syn::Type::Path(path) = payload else {
        return None;
    };
    if path.qself.is_some() || path.path.leading_colon.is_some() || path.path.segments.len() != 1 {
        return None;
    }
    path.path
        .segments
        .first()
        .filter(|segment| matches!(segment.arguments, syn::PathArguments::None))
        .and_then(|segment| schema_names.get(&segment.ident.to_string()))
}

// Serde's derived untagged enum buffers Content, which cannot represent
// arbitrary-precision integers or preserve RawValue through nested models.
// Try the generated payload types directly from the original-kind Value instead.
pub(super) fn untagged_deserialize_impl(
    item: &mut syn::ItemEnum,
    schema_name: Option<&str>,
    schema_names: &BTreeMap<String, String>,
) -> Item {
    let ident = &item.ident;
    let object_keys = if item.variants.iter().any(|variant| {
        matches!(&variant.fields, syn::Fields::Unnamed(fields)
            if fields.unnamed.len() == 1
                && named_payload_schema(&fields.unnamed.first().unwrap().ty, schema_names).is_some())
    }) {
        quote! {
            let object_keys = value.as_object().map(|object|
                object.keys().map(String::as_str).collect::<Vec<_>>());
        }
    } else { quote! {} };
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
                match named_payload_schema(payload, schema_names) {
                    Some(model) => quote! {
                        if crate::wire_schema::may_match_wire_model_shape(#model, object_keys.as_deref())
                            && let Ok(payload) = ::serde_json::from_value::<#payload>(value.clone()) {
                            return Ok(Self::#name(payload));
                        }
                    },
                    None => quote! {
                        if let Ok(payload) = ::serde_json::from_value::<#payload>(value.clone()) {
                            return Ok(Self::#name(payload));
                        }
                    },
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
    let schema_owner = match schema_name {
        Some(schema_name) => quote! { Some(#schema_name) },
        None => quote! { None },
    };
    let implementation = parse_quote! {
        impl<'de> ::serde::Deserialize<'de> for #ident {
            fn deserialize<D: ::serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                let value = crate::wire_schema::deserialize_wire_value(deserializer, #schema_owner)?;
                #object_keys
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

pub(super) fn deserialize_impl(item: &mut Item, schema_name: &str) -> Option<Item> {
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
                let value = crate::wire_schema::deserialize_wire_value(deserializer, Some(#schema_name))?;
                #raw
                // An empty message constructs itself without reading `raw`.
                #[allow(unused_variables)]
                let raw: Raw = ::serde_json::from_value(value).map_err(::serde::de::Error::custom)?;
                Ok(#construction)
            }
        }
    })
}
