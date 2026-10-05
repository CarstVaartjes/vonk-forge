//! The contract words the agent recognises in other parties' messages.
//!
//! The lifecycle and outcome vocabulary is defined once, in the shared contract,
//! and generated into `vonk_agent_protocol::generated`. The agent never spells one
//! of its words; where it has to recognise a code a helper or the Controller sent,
//! it compares against the generated enum's own spelling through these helpers.

use std::fmt::Display;

/// Whether `code` is exactly the wire spelling of `word`.
pub fn is<T: Display>(code: &str, word: T) -> bool {
    code == word.to_string()
}

/// Whether `code` is the wire spelling of any of `words`.
pub fn is_any<T: Display>(code: &str, words: &[T]) -> bool {
    words.iter().any(|word| is(code, word))
}
