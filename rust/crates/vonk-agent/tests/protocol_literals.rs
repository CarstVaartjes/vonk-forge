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
//! * no hand-built runtime preflight finding: a finding is built in one place,
//!   from a member of the contract's closed `RuntimePreflightFindingCode`, so a
//!   new free-string finding code does not compile and a second struct literal
//!   of the finding fails the ceiling below;
//! * the helper and protocol crates spell no vocabulary word either (their code
//!   builds from the same generated enums);
//! * the words of the agent's progress phase and helper response status
//!   (`ProgressPhase`, `HostHelperResponseStatus`) are guarded in every spelling,
//!   plain words included, because a phase is a plain English word:
//!   `"downloading"` is a member, not prose. A failure stage (`FailureStage`) is
//!   guarded by its type: every stage parameter takes the enum.
//!
//! The vocabulary is read from the exported wire schema, so a word added to the
//! contract is guarded without editing this test: besides the lifecycle enums
//! named below it includes every enum `ReasonCodeVocabulary` publishes.

use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};

const VOCABULARY_ENUMS: [&str; 11] = [
    "AgentResultState",
    "LifecycleState",
    "LifecycleEventKind",
    "OperatorActionName",
    "ErrorCategory",
    "WaitReason",
    "InvalidRequestReason",
    "SecurityRefusalReason",
    "FailureCode",
    "ProgressPhase",
    "HostHelperResponseStatus",
];

/// The enums whose every member is guarded, plain words included: how far work
/// has got and what the helper answered are short English words, so the separator
/// rule that finds the other enums' words would miss them.
///
/// `FailureStage` is guarded by its type instead: `Failure::stage`,
/// `UnknownEvidence::at` and every stage parameter take the enum, so a string
/// does not compile. A word scan would only flag the stage words that are also
/// a tool's argument (`stop`) or a directory (`runtime-cache`).
const PLAIN_WORD_ENUMS: [&str; 2] = ["ProgressPhase", "HostHelperResponseStatus"];

/// Words that are plain prose; only separated words are specific to the contract.
const PLAIN_WORDS_THAT_ARE_SPECIFIC: [&str; 2] = ["observing", "backoff"];

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// The enums the lifecycle contract names, and every enum the reason-code
/// carrier publishes (the carrier is the contract's list of closed code sets).
fn vocabulary_enums(schema: &serde_json::Value) -> BTreeSet<String> {
    let mut names: BTreeSet<String> = VOCABULARY_ENUMS
        .iter()
        .map(|name| (*name).to_owned())
        .collect();
    let carrier = schema["$defs"]["ReasonCodeVocabulary"]["properties"]
        .as_object()
        .expect("ReasonCodeVocabulary publishes the reason-code enums");
    for property in carrier.values() {
        let reference = property["$ref"].as_str().expect("an enum reference");
        names.insert(reference.rsplit('/').next().unwrap().to_owned());
    }
    assert!(names.contains("RuntimePreflightFindingCode") && names.contains("HelperErrorCode"));
    names
}

fn vocabulary() -> BTreeSet<String> {
    let schema: serde_json::Value = serde_json::from_slice(
        &fs::read(root().join("../vonk-agent-protocol/schema/wire.json")).unwrap(),
    )
    .unwrap();
    let mut words = BTreeSet::new();
    for name in vocabulary_enums(&schema) {
        let name = name.as_str();
        let members = schema["$defs"][name]["enum"]
            .as_array()
            .unwrap_or_else(|| panic!("{name} is not a contract enum"));
        for member in members {
            let word = member.as_str().unwrap();
            if word.contains(['-', '_', '.'])
                || PLAIN_WORDS_THAT_ARE_SPECIFIC.contains(&word)
                || PLAIN_WORD_ENUMS.contains(&name)
            {
                words.insert(word.to_owned());
            }
        }
    }
    assert!(words.contains("observing") && words.contains("operation_cancelled"));
    words
}

/// The module's code without its test modules, comments and doc comments.
///
/// Every `#[cfg(test)]` item is removed, wherever it stands: a file can carry a
/// small test module early and production code after it. Cutting at the first
/// one would leave the rest of the file unguarded.
fn production_code(source: &str) -> String {
    // A file-level cfg applies to every item, including out-of-line fixtures.
    if source.lines().map(str::trim).find(|line| !line.is_empty()) == Some("#![cfg(test)]") {
        return String::new();
    }
    let lines: Vec<&str> = source.lines().map(strip_comment).collect();
    let mut kept = Vec::new();
    let mut index = 0;
    while index < lines.len() {
        if lines[index].trim() != "#[cfg(test)]" {
            kept.push(lines[index]);
            index += 1;
            continue;
        }
        // The attribute applies to the next item: skip it through its closing
        // brace, or through its semicolon when it has no body.
        let mut depth = 0_i64;
        let mut opened = false;
        index += 1;
        while index < lines.len() {
            let (delta, ended) = brace_delta(lines[index]);
            depth += delta;
            opened |= lines[index].contains('{');
            index += 1;
            if (opened && depth <= 0) || (!opened && ended) {
                break;
            }
        }
    }
    kept.join("\n")
}

/// The change in brace depth of one line, outside string and char literals, and
/// whether a body-less item ended on it (a trailing `;` at depth zero).
fn brace_delta(line: &str) -> (i64, bool) {
    let mut delta = 0_i64;
    let mut quoted = false;
    let mut escaped = false;
    let chars: Vec<char> = line.chars().collect();
    let mut index = 0;
    while index < chars.len() {
        let character = chars[index];
        if quoted {
            if escaped {
                escaped = false;
            } else if character == '\\' {
                escaped = true;
            } else if character == '"' {
                quoted = false;
            }
        } else if character == '"' {
            quoted = true;
        } else if character == '\'' {
            // A char literal (`'{'`, `'\\''`) or a lifetime (`'a`): skip a literal.
            if chars.get(index + 2) == Some(&'\'') {
                index += 2;
            } else if chars.get(index + 1) == Some(&'\\') && chars.get(index + 3) == Some(&'\'') {
                index += 3;
            }
        } else if character == '{' {
            delta += 1;
        } else if character == '}' {
            delta -= 1;
        }
        index += 1;
    }
    (
        delta,
        !quoted && delta == 0 && line.trim_end().ends_with(';'),
    )
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

/// The sibling crates that also speak the agent protocol, scanned for `json!`.
const PROTOCOL_CRATES: [&str; 2] = ["vonk-agent-helper", "vonk-agent-protocol"];

/// `json!` that remains in a protocol crate's non-test code, with a ceiling that
/// only falls. Each entry builds a document that is not a protocol message.
const JSON_RESIDUE: [(&str, usize); 0] = [];

fn protocol_crate_sources() -> Vec<(String, String)> {
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
    let crates = root().join("..");
    let mut files = Vec::new();
    for name in PROTOCOL_CRATES {
        walk(&crates.join(name).join("src"), &mut files);
    }
    files.sort();
    files
        .into_iter()
        .map(|path| {
            let name = path
                .strip_prefix(&crates)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            (name, production_code(&fs::read_to_string(&path).unwrap()))
        })
        .collect()
}

#[test]
fn the_protocol_crates_build_no_message_from_loose_json_beyond_the_listed_residue() {
    let mut observed = std::collections::BTreeMap::new();
    for (name, code) in protocol_crate_sources() {
        let count = json_macro_sites(&code);
        if count > 0 {
            observed.insert(name, count);
        }
    }
    let ceiling: std::collections::BTreeMap<String, usize> = JSON_RESIDUE
        .iter()
        .map(|(name, count)| ((*name).to_owned(), *count))
        .collect();
    assert_eq!(
        observed, ceiling,
        "json! in the protocol crates must match JSON_RESIDUE exactly: a new site is a loose \
         protocol body (use the generated types); a removed one lowers the ceiling"
    );
}

/// Vocabulary-equal literals that remain in the agent, per file, with a ceiling
/// that only falls. It is empty: the source-policy recheck builds its findings from
/// the contract's `SourcePolicyCode` like the Controller's, and no other file
/// spells a word. A word that is a tool's own output goes to `FOREIGN_MEANINGS`
/// with the reason, never here.
const VOCABULARY_RESIDUE: [(&str, usize); 0] = [];

fn offenders_by_file(
    files: Vec<(String, String)>,
    vocabulary: &BTreeSet<String>,
) -> std::collections::BTreeMap<String, usize> {
    let mut observed = std::collections::BTreeMap::new();
    for (name, code) in files {
        let crate_path = format!("vonk-agent/{name}");
        let words = vocabulary_literals(&code, vocabulary)
            .into_iter()
            .filter(|word| !FOREIGN_MEANINGS.contains(&(crate_path.as_str(), word.as_str())))
            .count();
        if words > 0 {
            observed.insert(name, words);
        }
    }
    observed
}

#[test]
fn the_agent_spells_no_vocabulary_word_by_hand() {
    let vocabulary = vocabulary();
    let ceiling: std::collections::BTreeMap<String, usize> = VOCABULARY_RESIDUE
        .iter()
        .map(|(name, count)| ((*name).to_owned(), *count))
        .collect();
    assert_eq!(
        offenders_by_file(sources(), &vocabulary),
        ceiling,
        "use the generated contract enum instead of the string; VOCABULARY_RESIDUE only falls"
    );
}

/// The protocol crates' hand-written code: the generated declarations and the
/// schema document spell every member by construction.
fn handwritten_protocol_sources() -> Vec<(String, String)> {
    protocol_crate_sources()
        .into_iter()
        .filter(|(name, _)| !name.ends_with("/generated.rs") && !name.ends_with("/wire_schema.rs"))
        .collect()
}

/// A word that is not the contract's but happens to equal one, with the file it
/// is in and why: a tool's own output (systemd's `LoadState` is `not-found` for a
/// unit that is not installed, its `ActiveState` is `failed`) or a file's own
/// content (the helper's claim ledger marks a claim `pending`). A path is relative
/// to the crates directory.
const FOREIGN_MEANINGS: [(&str, &str); 3] = [
    ("vonk-agent-helper/src/package_rollback.rs", "not-found"),
    ("vonk-agent-helper/src/main.rs", "pending"),
    ("vonk-agent/src/recipe_builder.rs", "failed"),
];

#[test]
fn the_protocol_crates_spell_no_vocabulary_word_by_hand() {
    let vocabulary = vocabulary();
    let mut offenders = Vec::new();
    for (name, code) in handwritten_protocol_sources() {
        for word in vocabulary_literals(&code, &vocabulary) {
            if !FOREIGN_MEANINGS.contains(&(name.as_str(), word.as_str())) {
                offenders.push(format!("{name}: {word:?}"));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "use the generated contract enum instead of the string: {offenders:?}"
    );
}

/// Struct literals of the runtime preflight finding, with a ceiling that only
/// falls: the one constructor, which takes a member of the closed finding-code
/// enum. A second literal is a finding whose code could be any string.
const FINDING_LITERALS: [(&str, usize); 1] = [("src/runtime_preflight.rs", 1)];

fn finding_literal_sites(code: &str) -> usize {
    const NAMES: [&str; 2] = ["RuntimePreflightFinding", "Finding"];
    let mut sites = 0;
    let mut from = 0;
    while let Some(found) = code[from..].find("Finding {") {
        let end = from + found + "Finding".len();
        let start = code[..end]
            .rfind(|character: char| !(character.is_alphanumeric() || character == '_'))
            .map_or(0, |index| index + 1);
        let name = &code[start..end];
        // A declaration, an `impl` header or a function body that returns the
        // type is not a struct literal.
        let before = code[..start].trim_end();
        let declaration =
            before.ends_with("struct") || before.ends_with("impl") || before.ends_with("->");
        if NAMES.contains(&name) && !declaration {
            sites += 1;
        }
        from = end;
    }
    sites
}

#[test]
fn a_runtime_preflight_finding_is_built_in_one_place_from_the_contract_enum() {
    let mut observed = std::collections::BTreeMap::new();
    let agent = sources();
    let others = handwritten_protocol_sources();
    for (name, code) in agent.iter().chain(others.iter()) {
        let count = finding_literal_sites(code);
        if count > 0 {
            observed.insert(name.clone(), count);
        }
    }
    let ceiling: std::collections::BTreeMap<String, usize> = FINDING_LITERALS
        .iter()
        .map(|(name, count)| ((*name).to_owned(), *count))
        .collect();
    assert_eq!(
        observed, ceiling,
        "a finding is built by `finding` / `finding_with` in runtime_preflight.rs from a \
         RuntimePreflightFindingCode member; a new struct literal is a free-string code"
    );
}

#[test]
fn the_guard_finds_what_it_forbids() {
    let vocabulary = vocabulary();
    assert!(
        production_code("#![cfg(test)]\nfn fixture() { let _ = serde_json::json!({}); }")
            .is_empty()
    );
    let seeded = r#"
#[cfg(test)]
mod early_tests {
    fn fixture() { let _ = "observing"; let _ = serde_json::json!({}); }
}
fn wait() {
    let state = "observing"; // "stop-unconfirmed" in a comment is prose
    let body = serde_json::json!({"reason": state});
    let code = "operation_cancelled";
    let plain = "failed";
    let hand_built = Finding { capability: "x".into(), status: Status::Failed, code: "available".into() };
    let also = vonk_agent_protocol::runtime_preflight::RuntimePreflightFinding { code: "x".into() };
    let other = SourceFinding { code: "x" };
    fn made() -> Finding { build() }
}
pub struct Finding {}
impl Finding {}
#[cfg(test)]
mod tests {
    fn fixture() { let _ = "observing"; let _ = serde_json::json!({}); }
}
"#;
    let code = production_code(seeded);

    assert_eq!(json_macro_sites(&code), 1);
    assert_eq!(finding_literal_sites(&code), 2);
    assert_eq!(
        vocabulary_literals(&code, &vocabulary),
        ["observing", "operation_cancelled", "failed"]
    );
    // The phase, stage and helper-status words are guarded plain as well.
    let seeded = r#"let a = "downloading"; let b = "reconciling-installation"; let c = "rejected"; let d = "package-installed"; let e = "nothing";"#;
    assert_eq!(
        vocabulary_literals(seeded, &vocabulary),
        [
            "downloading",
            "reconciling-installation",
            "rejected",
            "package-installed"
        ]
    );
    // The finding code enum and the helper's codes are guarded like the rest.
    let seeded =
        r#"let a = "preflight_finding.available"; let b = "operation_io"; let c = "available";"#;
    assert_eq!(
        vocabulary_literals(seeded, &vocabulary),
        ["preflight_finding.available", "operation_io"]
    );
    // The finding code enum and the helper's codes are guarded like the rest.
    let seeded =
        r#"let a = "preflight_finding.available"; let b = "operation_io"; let c = "available";"#;
    assert_eq!(
        vocabulary_literals(seeded, &vocabulary),
        ["preflight_finding.available", "operation_io"]
    );
}

#[test]
fn split_operation_modules_use_generated_operation_words() {
    let schema: serde_json::Value =
        serde_json::from_str(include_str!("../../vonk-agent-protocol/schema/wire.json")).unwrap();
    let operations: BTreeSet<String> = schema["$defs"]["AgentOperation"]["enum"]
        .as_array()
        .unwrap()
        .iter()
        .map(|word| word.as_str().unwrap().to_owned())
        .collect();
    let offenders: Vec<_> = sources()
        .into_iter()
        .chain(handwritten_protocol_sources())
        .filter(|(name, _)| {
            name.starts_with("src/executor/")
                || name.starts_with("vonk-agent-helper/src/operations/")
        })
        .flat_map(|(name, source)| {
            vocabulary_literals(&source, &operations)
                .into_iter()
                .map(move |word| format!("{name}: {word}"))
        })
        .collect();
    assert!(
        offenders.is_empty(),
        "use AgentOperation for operation family words: {offenders:?}"
    );
}
