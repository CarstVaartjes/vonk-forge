//! The one form a result's prose leaves the agent in.
//!
//! A reason, summary, detail, diagnostic or recovery is a sentence a person
//! reads: it is never an identity, a path the Controller acts on or authority.
//! The Controller refuses a host path in a result (`filesystem path values are
//! not allowed`), so every such leaf, at any depth, has its host paths replaced
//! by [`HOST_PATH_MARKER`] in the bytes sent, by the rule of the shared vector
//! `host-path-redaction-v1.json` the Controller redacts by too.  The retained
//! result keeps its own words, and the captured process streams (bounded
//! evidence the contract admits paths in) are untouched.

use serde_json::Value;
use vonk_agent_protocol::generated::AgentResult;

/// What stands in for a host path inside prose.
pub const HOST_PATH_MARKER: &str = "<host-path>";

/// The shortest bound of any prose leaf (`recovery`): a redaction that makes a
/// sentence longer is cut to this or its own length, so it stays within the
/// bound the original met.
const SHORTEST_PROSE_BOUND: usize = 128;

/// The leaf names that are prose wherever they occur in a result.
const PROSE_LEAVES: [&str; 5] = ["reason", "summary", "detail", "diagnostic", "recovery"];

/// `text` with every path-like token replaced by [`HOST_PATH_MARKER`].
///
/// A token runs between whitespace, quotes, brackets and `,;=:`; one holding a
/// path separator is replaced whole, so `key=` before it and `:line` after it
/// are kept.
pub fn redact_host_paths(text: &str) -> String {
    fn flush(token: &mut String, out: &mut String) {
        if token.contains(['/', '\\']) {
            out.push_str(HOST_PATH_MARKER);
        } else {
            out.push_str(token);
        }
        token.clear();
    }
    let mut out = String::with_capacity(text.len());
    let mut token = String::new();
    for character in text.chars() {
        if character.is_whitespace() || "'\"`()[]{},;=:".contains(character) {
            flush(&mut token, &mut out);
            out.push(character);
        } else {
            token.push(character);
        }
    }
    flush(&mut token, &mut out);
    let bound = text.chars().count().max(SHORTEST_PROSE_BOUND);
    out.chars().take(bound).collect()
}

fn redact_prose(value: &mut Value) {
    match value {
        Value::Array(items) => items.iter_mut().for_each(redact_prose),
        Value::Object(fields) => {
            for (key, field) in fields.iter_mut() {
                match field {
                    Value::String(text)
                        if PROSE_LEAVES.contains(&key.as_str()) && text.contains(['/', '\\']) =>
                    {
                        *text = redact_host_paths(text);
                    }
                    _ => redact_prose(field),
                }
            }
        }
        _ => {}
    }
}

/// The result as it is sent: prose leaves without host paths, all else as is.
///
/// A result whose redacted form does not read back (which no prose change can
/// cause) is sent as it was, so the Controller still answers it.
pub fn wire_result(result: &AgentResult) -> AgentResult {
    let Ok(mut value) = serde_json::to_value(result) else {
        return result.clone();
    };
    redact_prose(&mut value);
    serde_json::from_value(value).unwrap_or_else(|_| result.clone())
}

#[cfg(test)]
mod tests;
