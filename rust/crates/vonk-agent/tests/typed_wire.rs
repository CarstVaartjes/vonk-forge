//! Guard: our protocol and structured data are typed all the way down.
//!
//! Every message, claim, plan, result, receipt, helper request and state file is
//! a type generated from the shared Pydantic contract, or a local type with a
//! declared shape. An untyped JSON document (`serde_json::Value`) in non-test
//! code is therefore either:
//!
//! * inside generated code (the deserializer prologue that hands the document to
//!   the schema validator), or
//! * inside one of the few passthrough modules below, each of which names the
//!   reason the data is not ours to type.
//!
//! Passthrough modules name the external boundary and its reason. Every other
//! module must use declared types. `json!` is never allowed in non-test code.

use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

/// Passthrough modules, with the external boundary each owns.
const PASSTHROUGH: &[(&str, &str)] = &[
    (
        "vonk-agent-protocol/src/passthrough.rs",
        "the typed-to-document boundary: canonical bytes and validating or \
         round-tripping a generated value; the document never leaves the newtype",
    ),
    (
        "vonk-agent-protocol/src/wire_schema.rs",
        "the JSON Schema interpreter behind every generated deserializer; its \
         subject is the schema document and the instance under validation",
    ),
];

/// Identifiers that mean "an untyped JSON document is in play".
const UNTYPED: [&str; 3] = ["Value", "to_value", "from_value"];

fn crates() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..")
}

fn rust_files(directory: &Path, found: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(directory).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            rust_files(&path, found);
        } else if path.extension().is_some_and(|extension| extension == "rs") {
            found.push(path);
        }
    }
}

/// Source with comments, string literals and `#[cfg(test)]` items removed.
fn production_code(source: &str) -> String {
    let stripped = strip_comments_and_strings(source);
    if stripped
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        == Some("#![cfg(test)]")
    {
        return String::new();
    }
    strip_test_items(&stripped)
}

fn strip_comments_and_strings(source: &str) -> String {
    let characters: Vec<char> = source.chars().collect();
    let mut output = String::with_capacity(source.len());
    let mut index = 0;
    while index < characters.len() {
        let character = characters[index];
        let next = characters.get(index + 1).copied();
        if character == '/' && next == Some('/') {
            while index < characters.len() && characters[index] != '\n' {
                index += 1;
            }
        } else if character == '/' && next == Some('*') {
            let mut depth = 0;
            while index < characters.len() {
                if characters[index] == '/' && characters.get(index + 1) == Some(&'*') {
                    depth += 1;
                    index += 2;
                } else if characters[index] == '*' && characters.get(index + 1) == Some(&'/') {
                    depth -= 1;
                    index += 2;
                    if depth == 0 {
                        break;
                    }
                } else {
                    if characters[index] == '\n' {
                        output.push('\n');
                    }
                    index += 1;
                }
            }
        } else if character == 'r'
            && matches!(next, Some('"' | '#'))
            && raw_string(&characters, index).is_some()
        {
            let end = raw_string(&characters, index).unwrap();
            output.extend(characters[index..end].iter().filter(|c| **c == '\n'));
            index = end;
        } else if character == '"' {
            index += 1;
            while index < characters.len() && characters[index] != '"' {
                if characters[index] == '\\' {
                    index += 1;
                }
                if characters.get(index) == Some(&'\n') {
                    output.push('\n');
                }
                index += 1;
            }
            index += 1;
            output.push_str("\"\"");
        } else if character == '\'' && char_literal_end(&characters, index).is_some() {
            index = char_literal_end(&characters, index).unwrap();
        } else {
            output.push(character);
            index += 1;
        }
    }
    output
}

/// The index just past a raw string literal starting at `start`, if one starts there.
fn raw_string(characters: &[char], start: usize) -> Option<usize> {
    let mut index = start + 1;
    let mut hashes = 0;
    while characters.get(index) == Some(&'#') {
        hashes += 1;
        index += 1;
    }
    if characters.get(index) != Some(&'"') {
        return None;
    }
    let preceded_by_identifier =
        start > 0 && (characters[start - 1].is_alphanumeric() || characters[start - 1] == '_');
    if preceded_by_identifier {
        return None;
    }
    index += 1;
    while index < characters.len() {
        if characters[index] == '"'
            && (0..hashes).all(|offset| characters.get(index + 1 + offset) == Some(&'#'))
        {
            return Some(index + 1 + hashes);
        }
        index += 1;
    }
    None
}

/// The index just past a char literal such as `'a'` or `'\n'`; lifetimes are not literals.
fn char_literal_end(characters: &[char], start: usize) -> Option<usize> {
    match (characters.get(start + 1), characters.get(start + 2)) {
        (Some('\\'), _) => {
            let mut index = start + 2;
            while index < characters.len() && characters[index] != '\'' {
                index += 1;
            }
            Some(index + 1)
        }
        (Some(_), Some('\'')) => Some(start + 3),
        _ => None,
    }
}

/// Remove every `#[cfg(test)]` item, including the whole body of a test module.
fn strip_test_items(source: &str) -> String {
    const MARKER: &str = "#[cfg(test)]";
    let mut output = String::new();
    let mut rest = source;
    while let Some(position) = rest.find(MARKER) {
        output.push_str(&rest[..position]);
        let item = &rest[position + MARKER.len()..];
        let end = item
            .find(['{', ';'])
            .map(|open| {
                if item.as_bytes()[open] == b';' {
                    open + 1
                } else {
                    let mut depth = 0;
                    let mut close = open;
                    for (offset, byte) in item.bytes().enumerate().skip(open) {
                        match byte {
                            b'{' => depth += 1,
                            b'}' => {
                                depth -= 1;
                                if depth == 0 {
                                    close = offset + 1;
                                    break;
                                }
                            }
                            _ => {}
                        }
                    }
                    close
                }
            })
            .unwrap_or(item.len());
        rest = &item[end..];
    }
    output.push_str(rest);
    output
}

fn identifiers(code: &str) -> Vec<(&str, bool)> {
    let bytes = code.as_bytes();
    let mut found = Vec::new();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index].is_ascii_alphabetic() || bytes[index] == b'_' {
            let start = index;
            while index < bytes.len()
                && (bytes[index].is_ascii_alphanumeric() || bytes[index] == b'_')
            {
                index += 1;
            }
            let macro_call = bytes.get(index) == Some(&b'!');
            found.push((&code[start..index], macro_call));
        } else {
            index += 1;
        }
    }
    found
}

fn has_untyped_json(code: &str) -> bool {
    identifiers(code)
        .into_iter()
        .any(|(name, _)| UNTYPED.contains(&name))
}

fn has_json_macro(code: &str) -> bool {
    identifiers(code)
        .into_iter()
        .any(|(name, macro_call)| name == "json" && macro_call)
}

/// Non-test code of every workspace crate except the code generator (a build
/// tool that reads schema documents, not a protocol participant) and the
/// generated declarations themselves.
fn workspace_production() -> BTreeMap<String, String> {
    let mut files = Vec::new();
    for entry in fs::read_dir(crates()).unwrap() {
        let path = entry.unwrap().path();
        if path
            .file_name()
            .is_some_and(|name| name == "vonk-wire-codegen")
        {
            continue;
        }
        if path.join("src").is_dir() {
            rust_files(&path.join("src"), &mut files);
        }
    }
    files
        .into_iter()
        .filter(|path| path.file_name().is_some_and(|name| name != "generated.rs"))
        .map(|path| {
            let name = path
                .strip_prefix(crates())
                .unwrap()
                .to_string_lossy()
                .into_owned();
            let code = production_code(&fs::read_to_string(&path).unwrap());
            (name, code)
        })
        .collect()
}

#[test]
fn non_test_code_builds_no_json_literal() {
    let offenders: Vec<_> = workspace_production()
        .into_iter()
        .filter_map(|(name, code)| {
            has_json_macro(&code).then(|| format!("{name}: json! outside declared types"))
        })
        .collect();
    assert!(
        offenders.is_empty(),
        "build the value from a generated contract type or a declared struct, not json!: {offenders:?}"
    );
}

#[test]
fn untyped_json_stays_inside_declared_passthrough_modules() {
    let production = workspace_production();
    let mut offenders = Vec::new();
    for (name, code) in &production {
        if has_untyped_json(code) && !PASSTHROUGH.iter().any(|(path, _)| name == path) {
            offenders.push(format!(
                "{name}: untyped JSON outside an external passthrough"
            ));
        }
    }
    for (path, reason) in PASSTHROUGH {
        assert!(
            !reason.trim().is_empty(),
            "passthrough {path} needs a reason"
        );
        assert!(
            production
                .get(*path)
                .is_some_and(|code| has_untyped_json(code)),
            "stale passthrough entry {path} ({reason})"
        );
    }
    assert!(offenders.is_empty(), "{offenders:#?}");
}

#[test]
fn generated_wire_types_carry_no_untyped_json_field() {
    let source = vonk_agent_protocol::GENERATED_SOURCE;
    // The only untyped document in generated code is the one each deserializer
    // reads before handing it to the schema validator.
    let offenders: Vec<_> = source
        .lines()
        .enumerate()
        .filter(|(_, line)| line.contains("serde_json::Value"))
        .filter(|(_, line)| {
            !line.contains(
                "<::serde_json::Value as ::serde::Deserialize>::deserialize(deserializer)",
            )
        })
        .map(|(number, line)| format!("generated.rs:{}: {}", number + 1, line.trim()))
        .collect();
    assert!(
        offenders.is_empty(),
        "a contract field is typed as an arbitrary JSON value; give it a declared model: {offenders:#?}"
    );
}

#[test]
fn the_guard_detects_untyped_json_without_counting_sites() {
    assert!(has_untyped_json(&production_code(
        "fn f() { let a: serde_json::Value; }"
    )));
    assert!(has_json_macro(&production_code(
        "fn f() { let a = serde_json::json!({}); }"
    )));
    assert!(!has_untyped_json(&production_code(
        "// Value\nfn f() { let a: Typed; }"
    )));
    assert!(!has_json_macro(&production_code(
        "#[cfg(test)] mod tests { fn f() { json!({}); } }"
    )));
}
