//! Guard: the agent speaks the protocol through the generated contract types only.
//!
//! The lifecycle and outcome vocabulary has one definition, the shared contract
//! (`vonk_agent_protocol::lifecycle_vocabulary`), generated into
//! `vonk-agent-protocol`. Two rules keep the agent from growing a second one:
//!
//! * no `json!` in non-test agent code, so an operation result or failure body is
//!   always a generated type and never an ad-hoc document (the shape that let an
//!   untyped `{"reason": ...}` waiting body bypass the contract);
//! * no string literal in non-test agent code equal to a word of the contract's
//!   vocabulary (a state, a wait reason, a failure code, a security-refusal
//!   code...): use the generated enum.
//!
//! The vocabulary is read from the exported wire schema, so a word added to the
//! contract is guarded without editing this test.

use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};

const VOCABULARY_ENUMS: [&str; 9] = [
    "AgentResultState",
    "LifecycleState",
    "LifecycleEventKind",
    "OperatorActionName",
    "ErrorCategory",
    "WaitReason",
    "InvalidRequestReason",
    "SecurityRefusalReason",
    "FailureCode",
];

/// Words that are plain prose; only separated words are specific to the contract.
const PLAIN_WORDS_THAT_ARE_SPECIFIC: [&str; 2] = ["observing", "backoff"];

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn vocabulary() -> BTreeSet<String> {
    let schema: serde_json::Value = serde_json::from_slice(
        &fs::read(root().join("../vonk-agent-protocol/schema/wire.json")).unwrap(),
    )
    .unwrap();
    let mut words = BTreeSet::new();
    for name in VOCABULARY_ENUMS {
        let members = schema["$defs"][name]["enum"]
            .as_array()
            .unwrap_or_else(|| panic!("{name} is not a contract enum"));
        for member in members {
            let word = member.as_str().unwrap();
            if word.contains(['-', '_', '.']) || PLAIN_WORDS_THAT_ARE_SPECIFIC.contains(&word) {
                words.insert(word.to_owned());
            }
        }
    }
    assert!(words.contains("waiting-for-operator") && words.contains("operation_cancelled"));
    words
}

/// The module's code without its test module, comments and doc comments.
fn production_code(source: &str) -> String {
    let production = source
        .split("\n#[cfg(test)]\nmod ")
        .next()
        .unwrap_or(source);
    production
        .lines()
        .map(strip_comment)
        .collect::<Vec<_>>()
        .join("\n")
}

fn strip_comment(line: &str) -> &str {
    let mut quoted = false;
    let mut escaped = false;
    let bytes = line.as_bytes();
    for (index, byte) in bytes.iter().enumerate() {
        match byte {
            b'\\' if quoted && !escaped => {
                escaped = true;
                continue;
            }
            b'"' if !escaped => quoted = !quoted,
            b'/' if !quoted && bytes.get(index + 1) == Some(&b'/') => return &line[..index],
            _ => {}
        }
        escaped = false;
    }
    line
}

fn string_literals(code: &str) -> Vec<String> {
    let mut found = Vec::new();
    let mut chars = code.chars().peekable();
    while let Some(character) = chars.next() {
        if character != '"' {
            continue;
        }
        let mut literal = String::new();
        while let Some(next) = chars.next() {
            match next {
                '\\' => {
                    chars.next();
                }
                '"' => break,
                other => literal.push(other),
            }
        }
        found.push(literal);
    }
    found
}

fn sources() -> Vec<(String, String)> {
    fn walk(directory: &Path, found: &mut Vec<PathBuf>) {
        for entry in fs::read_dir(directory).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                walk(&path, found);
            } else if path.extension().is_some_and(|extension| extension == "rs") {
                found.push(path);
            }
        }
    }
    let mut files = Vec::new();
    walk(&root().join("src"), &mut files);
    files.sort();
    files
        .into_iter()
        .map(|path| {
            let name = path
                .strip_prefix(root())
                .unwrap()
                .to_string_lossy()
                .into_owned();
            let code = production_code(&fs::read_to_string(&path).unwrap());
            (name, code)
        })
        .collect()
}

fn json_macro_sites(code: &str) -> usize {
    code.matches("json!(").count() + code.matches("json! (").count()
}

fn vocabulary_literals(code: &str, vocabulary: &BTreeSet<String>) -> Vec<String> {
    string_literals(code)
        .into_iter()
        .filter(|literal| vocabulary.contains(literal))
        .collect()
}

#[test]
fn the_agent_builds_no_protocol_body_from_loose_json() {
    let offenders: Vec<_> = sources()
        .into_iter()
        .filter_map(|(name, code)| {
            let count = json_macro_sites(&code);
            (count > 0).then(|| format!("{name}: {count} json! site(s)"))
        })
        .collect();
    assert!(
        offenders.is_empty(),
        "build the result from the generated contract types (see src/outcome.rs), not json!: {offenders:?}"
    );
}

#[test]
fn the_agent_spells_no_vocabulary_word_by_hand() {
    let vocabulary = vocabulary();
    let offenders: Vec<_> = sources()
        .into_iter()
        .flat_map(|(name, code)| {
            vocabulary_literals(&code, &vocabulary)
                .into_iter()
                .map(move |word| format!("{name}: {word:?}"))
        })
        .collect();
    assert!(
        offenders.is_empty(),
        "use the generated contract enum instead of the string: {offenders:?}"
    );
}

#[test]
fn the_guard_finds_what_it_forbids() {
    let vocabulary = vocabulary();
    let seeded = r#"
fn wait() {
    let state = "waiting-for-operator"; // "stop-unconfirmed" in a comment is prose
    let body = serde_json::json!({"reason": state});
    let code = "operation_cancelled";
    let plain = "failed";
}
#[cfg(test)]
mod tests {
    fn fixture() { let _ = "waiting-for-operator"; let _ = serde_json::json!({}); }
}
"#;
    let code = production_code(seeded);

    assert_eq!(json_macro_sites(&code), 1);
    assert_eq!(
        vocabulary_literals(&code, &vocabulary),
        ["waiting-for-operator", "operation_cancelled"]
    );
}
