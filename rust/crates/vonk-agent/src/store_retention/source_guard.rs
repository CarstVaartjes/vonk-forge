#![cfg(test)]
//! Parse production Rust rather than guessing the spelling/spacing of joins.
use std::collections::{BTreeMap, BTreeSet};
use syn::{
    Expr, Item, Lit, Member,
    visit::{self, Visit},
};
use vonk_agent_protocol::store_retention::StoreRoot;

fn test_only(attributes: &[syn::Attribute]) -> bool {
    fn production_possible(meta: &syn::Meta) -> bool {
        match meta {
            syn::Meta::Path(path) if path.is_ident("test") => false,
            syn::Meta::List(list) if list.path.is_ident("all") || list.path.is_ident("any") => {
                let terms = list
                    .parse_args_with(
                        syn::punctuated::Punctuated::<syn::Meta, syn::Token![,]>::parse_terminated,
                    )
                    .unwrap();
                if list.path.is_ident("all") {
                    terms.iter().all(production_possible)
                } else {
                    terms.iter().any(production_possible)
                }
            }
            // Other conditions might hold in production, including not(test).
            _ => true,
        }
    }
    attributes.iter().any(|attribute| {
        attribute.path().segments.last().is_some_and(|segment| segment.ident == "test")
            || matches!(&attribute.meta, syn::Meta::List(list)
                if list.path.is_ident("cfg") && !production_possible(&list.parse_args::<syn::Meta>().unwrap()))
    })
}

fn attributes(item: &Item) -> &[syn::Attribute] {
    match item {
        Item::Fn(item) => &item.attrs,
        Item::Mod(item) => &item.attrs,
        Item::Impl(item) => &item.attrs,
        Item::Const(item) => &item.attrs,
        Item::Static(item) => &item.attrs,
        _ => &[],
    }
}

pub(super) fn constants(text: &str) -> BTreeMap<String, String> {
    struct Constants(BTreeMap<String, String>);
    impl<'ast> Visit<'ast> for Constants {
        fn visit_item(&mut self, item: &'ast Item) {
            if !test_only(attributes(item)) {
                visit::visit_item(self, item);
            }
        }
        fn visit_item_const(&mut self, item: &'ast syn::ItemConst) {
            if let Expr::Lit(value) = &*item.expr
                && let Lit::Str(value) = &value.lit
            {
                self.0.insert(item.ident.to_string(), value.value());
            }
        }
    }
    let mut constants = Constants(BTreeMap::new());
    constants.visit_file(&syn::parse_file(text).unwrap());
    constants.0
}

fn data_root(expression: &Expr, default: StoreRoot) -> Option<StoreRoot> {
    let name = match expression {
        Expr::Path(path) => path.path.get_ident().map(ToString::to_string),
        Expr::Field(field) => match &field.member {
            Member::Named(name) => Some(name.to_string()),
            Member::Unnamed(_) => None,
        },
        Expr::Reference(reference) => return data_root(&reference.expr, default),
        Expr::Paren(paren) => return data_root(&paren.expr, default),
        _ => None,
    };
    match name.as_deref() {
        Some("agent_data" | "data_dir") => Some(StoreRoot::Agent),
        Some("data_root" | "data" | "data_root_file") => Some(default),
        _ => None,
    }
}

pub(super) fn stores(
    text: &str,
    constants: &BTreeMap<String, String>,
    default: StoreRoot,
) -> BTreeSet<(StoreRoot, String)> {
    struct Stores<'a> {
        constants: &'a BTreeMap<String, String>,
        names: BTreeSet<(StoreRoot, String)>,
        default: StoreRoot,
    }
    impl Stores<'_> {
        fn path(&mut self, expression: &Expr, root: StoreRoot) {
            match expression {
                Expr::Lit(syn::ExprLit {
                    lit: Lit::Str(value),
                    ..
                }) => {
                    self.names
                        .insert((root, value.value().split('/').next().unwrap().to_owned()));
                }
                Expr::Path(path) => {
                    let name = path.path.segments.last().unwrap().ident.to_string();
                    self.names.insert((
                        root,
                        self.constants
                            .get(&name)
                            .unwrap_or_else(|| panic!("unresolved data-root store {name}"))
                            .split('/')
                            .next()
                            .unwrap()
                            .to_owned(),
                    ));
                }
                Expr::Macro(expression) if expression.mac.path.is_ident("format") => {
                    use syn::parse::Parser;
                    let arguments =
                        syn::punctuated::Punctuated::<Expr, syn::Token![,]>::parse_terminated
                            .parse2(expression.mac.tokens.clone())
                            .unwrap();
                    let Some(Expr::Lit(literal)) = arguments.first() else {
                        panic!("literal store format required")
                    };
                    let Lit::Str(format) = &literal.lit else {
                        panic!("string store format required")
                    };
                    let mut pattern = String::new();
                    let mut field = false;
                    for character in format.value().chars() {
                        match character {
                            '{' => {
                                field = true;
                                pattern.push('*');
                            }
                            '}' => field = false,
                            _ if !field => pattern.push(character),
                            _ => {}
                        }
                    }
                    assert!(
                        !pattern.starts_with('*'),
                        "data-root store must have a static name"
                    );
                    self.names
                        .insert((root, pattern.split('/').next().unwrap().to_owned()));
                }
                // An unrecognised expression fails the guard, never disappears.
                _ => {
                    self.names
                        .insert((root, "unresolved dynamic data-root path".to_owned()));
                }
            }
        }
    }
    impl<'ast> Visit<'ast> for Stores<'_> {
        fn visit_item(&mut self, item: &'ast Item) {
            if !test_only(attributes(item)) {
                visit::visit_item(self, item);
            }
        }
        fn visit_expr_method_call(&mut self, call: &'ast syn::ExprMethodCall) {
            if call.method == "join"
                && let Some(root) = data_root(&call.receiver, self.default)
            {
                self.path(call.args.first().expect("join argument"), root);
            }
            visit::visit_expr_method_call(self, call);
        }
        fn visit_lit_str(&mut self, value: &'ast syn::LitStr) {
            for (prefix, root) in [
                ("/var/lib/vonk-forge-agent/", StoreRoot::Agent),
                ("/var/lib/vonk-forge/", StoreRoot::Helper),
            ] {
                if let Some(path) = value.value().strip_prefix(prefix) {
                    self.names
                        .insert((root, path.split('/').next().unwrap().to_owned()));
                }
            }
        }
        fn visit_expr_call(&mut self, call: &'ast syn::ExprCall) {
            if let Expr::Path(function) = &*call.func
                && function.path.segments.last().is_some_and(|segment| {
                    matches!(
                        segment.ident.to_string().as_str(),
                        "atomic_private_write"
                            | "open_or_create_directory"
                            | "owned_directory"
                            | "managed_path"
                            | "openat"
                            | "openat2"
                            | "rotating_page"
                    )
                })
                && let Some(root) = call
                    .args
                    .first()
                    .and_then(|argument| data_root(argument, self.default))
                && call.args.len() > 1
                && (matches!(call.args.get(1), Some(Expr::Lit(value)) if matches!(value.lit, Lit::Str(_)))
                    || matches!(call.args.get(1), Some(Expr::Path(path)) if path.path.segments.last().is_some_and(|segment| self.constants.contains_key(&segment.ident.to_string()))))
            {
                self.path(&call.args[1], root);
            }
            visit::visit_expr_call(self, call);
        }
    }
    let mut stores = Stores {
        constants,
        names: BTreeSet::new(),
        default,
    };
    let file = syn::parse_file(text).unwrap();
    if !test_only(&file.attrs) {
        stores.visit_file(&file);
    }
    stores.names
}

#[test]
fn fields_constants_and_multiline_joins_are_checked_but_test_bodies_are_not() {
    // Catches ignoring field receivers/constants or including inline test code.
    let text = r#"
        const DIRECTORY: &str = "job-exits";
        fn write() {
            self.roots.data
                .join("new-helper-store");
            data_root.join(DIRECTORY);
            config.data_dir.join("credentials");
            verify_private_directory(data, "a-diagnostic-label");
        }
        #[cfg(test)] mod tests {
            fn fixture() { roots.data.join("unrelated-fence-bytes"); }
        }
        #[cfg(all(test, target_os = "linux"))] mod linux_tests {
            fn fixture() { data_root.join("inline-only"); }
        }
        #[test] fn fixture() { roots.data.join("independent-container-effect"); }
    "#;
    assert_eq!(
        stores(text, &constants(text), StoreRoot::Helper),
        [
            (StoreRoot::Helper, "new-helper-store"),
            (StoreRoot::Helper, "job-exits"),
            (StoreRoot::Agent, "credentials")
        ]
        .map(|(root, name)| (root, name.to_owned()))
        .into()
    );
}

/// Packages use literal absolute paths and mktemp's six-X template.
pub(super) fn package_stores(text: &str) -> BTreeSet<(StoreRoot, String)> {
    let mut names = BTreeSet::new();
    for (prefix, root) in [
        ("/var/lib/vonk-forge-agent/", StoreRoot::Agent),
        ("/var/lib/vonk-forge/", StoreRoot::Helper),
    ] {
        for (index, _) in text.match_indices(prefix) {
            let rest = &text[index + prefix.len()..];
            let name = rest
                .split(|c: char| !(c.is_ascii_alphanumeric() || "-_.".contains(c)))
                .next()
                .unwrap();
            let name = name
                .strip_suffix(".XXXXXX")
                .map_or_else(|| name.to_owned(), |prefix| format!("{prefix}.*"));
            if !name.is_empty() {
                names.insert((root, name));
            }
        }
    }
    names
}

#[test]
fn package_paths_and_crash_left_templates_are_not_lost_at_the_root_prefix() {
    // Catches inspecting match_indices' matched prefix instead of its suffix.
    assert_eq!(
        package_stores(
            "install /var/lib/vonk-forge/new-package-store; mktemp /var/lib/vonk-forge-agent/.receipt.XXXXXX"
        ),
        [
            (StoreRoot::Helper, "new-package-store".to_owned()),
            (StoreRoot::Agent, ".receipt.*".to_owned())
        ]
        .into()
    );
}
