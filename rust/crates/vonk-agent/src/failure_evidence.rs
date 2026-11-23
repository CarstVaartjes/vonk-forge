//! Bounded, sanitized observations captured locally after an operation fails.
//! Fixed local systemd query only; no network, environment dumps or directory scans.

use crate::outcome::{Failure, RefusalBound};
use chrono::Utc;
use std::{
    collections::VecDeque,
    fs::File,
    io::Read,
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};
use vonk_agent_protocol::generated::AgentOperation;

const LOG_LINES: usize = 32;
const TRUNCATED_LOG_MARKER: &[u8] = b"[earlier diagnostic output truncated]";

pub use vonk_agent_protocol::failure_evidence::{
    EXIT_CAUSE_FIELD, EXIT_SUMMARY_EXCEPTION_FIELD, ExitCause, FailureCategory, FailureDiagnostics,
    FailureLogTail, FailureProperty,
};
use vonk_agent_protocol::failure_evidence::{LOG_TAIL_BYTES as LOG_BYTES, is_omission_marker};

pub use vonk_agent_protocol::generated::HostHelperProcessLogs as FailureProcessLogs;

/// Credential names whose *value* makes a line sensitive.
///
/// The names are matched as whole words followed by an assignment, because the
/// words themselves appear in the configuration an operator has to read: a bare
/// `token` alternative replaced every line mentioning `num_speculative_tokens`
/// with `[redacted diagnostic line]`, which is exactly the launch configuration
/// a failed workload is diagnosed from.
const CREDENTIAL_NAMES: [&str; 12] = [
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "credential",
    "api_key",
    "api-key",
    "private_key",
    "private-key",
    "private key",
];

/// A name is bounded by non-alphanumeric characters, so `HF_TOKEN=...` and
/// `API_TOKEN=...` are credentials while `num_speculative_tokens` and
/// `tokenizer_config.json` are configuration: only an adjacent *letter or
/// digit* glues a name into a longer word.
fn is_word_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric()
}

/// True when `rest` begins with an assignment that carries a value.
fn carries_value(rest: &str) -> bool {
    let rest = rest.trim_start();
    match rest.strip_prefix(':').or_else(|| rest.strip_prefix('=')) {
        Some(value) => !value.trim().is_empty(),
        None => false,
    }
}

/// True when the line carries a credential value rather than the topic's name.
#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
pub fn names_credential_value(line: &str) -> bool {
    let lower = line.to_lowercase();
    if lower.contains("-----begin") || lower.contains("-----end") {
        return true;
    }
    // An authentication scheme always carries its value inline.
    if lower
        .split_whitespace()
        .collect::<Vec<_>>()
        .windows(2)
        .any(|pair| matches!(pair[0].trim_end_matches(':'), "bearer" | "basic"))
    {
        return true;
    }
    let bytes = lower.as_bytes();
    CREDENTIAL_NAMES.iter().any(|name| {
        let mut from = 0;
        while let Some(index) = lower[from..].find(name) {
            let start = from + index;
            let end = start + name.len();
            let whole_word = (start == 0 || !is_word_byte(bytes[start - 1]))
                && (end == lower.len() || !is_word_byte(bytes[end]));
            if whole_word && carries_value(&lower[end..]) {
                return true;
            }
            from = end;
        }
        false
    })
}

#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
pub fn sanitize_text(text: &str) -> String {
    let text: String = text
        .chars()
        .filter(|character| {
            (!character.is_control() || matches!(character, '\n' | '\t'))
                && !matches!(*character as u32, 0x200b..=0x200f | 0x202a..=0x202e | 0x2060..=0x206f)
        })
        .collect();
    text.lines()
        .map(|line| {
            if names_credential_value(line) {
                return "[redacted diagnostic line]".to_owned();
            }
            // Whitespace is kept as written: a traceback's indentation is
            // its structure.  Every piece ends in at most one whitespace
            // character, which follows the word it is split from.
            line.split_inclusive(char::is_whitespace)
                .map(|piece| {
                    let word = piece.trim_end_matches(char::is_whitespace);
                    redact_word(word) + &piece[word.len()..]
                })
                .collect()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn redact_word(word: &str) -> String {
    if let Ok(mut url) = url::Url::parse(word)
        && matches!(url.scheme(), "http" | "https")
    {
        #[expect(
            clippy::let_underscore_must_use,
            reason = "clearing credentials from a URL that cannot carry them reports nothing to act on"
        )]
        let _ = url.set_username("");
        #[expect(
            clippy::let_underscore_must_use,
            reason = "clearing credentials from a URL that cannot carry them reports nothing to act on"
        )]
        let _ = url.set_password(None);
        url.set_query(None);
        url.set_fragment(None);
        return url.to_string();
    }
    redact_opaque_runs(word)
}

/// An opaque value is a 40+ character run of base64-ish characters within a
/// word, bounded by anything else (a quote, comma, colon, bracket): a quote or
/// comma around a value never hides it. A run that touches a `.` is part of a
/// dotted name (a path, a module, a host) and is kept whole, as before.
fn redact_opaque_runs(word: &str) -> String {
    let charset = |c: char| c.is_ascii_alphanumeric() || "+/=_-".contains(c);
    let mut out = String::with_capacity(word.len());
    let mut segment = String::new();
    let flush = |segment: &mut String, out: &mut String| {
        if segment.len() >= 40 && segment.chars().all(charset) {
            out.push_str(&redact_opaque(segment));
        } else {
            out.push_str(segment);
        }
        segment.clear();
    };
    for c in word.chars() {
        if charset(c) || c == '.' {
            segment.push(c);
        } else {
            flush(&mut segment, &mut out);
            out.push(c);
        }
    }
    flush(&mut segment, &mut out);
    out
}

/// One opaque value. It is kept only as a `name=value` field whose value is a
/// refusal code of the contract (its own enum decides):
/// `error_code=helper_request_plan_binding_invalid`. Anything else, name
/// included, is redacted whole: a UUID or a short value behind a name may be a
/// credential, and so may the name.
fn redact_opaque(token: &str) -> String {
    match token.split_once('=') {
        Some((name, value))
            if is_field_name(name)
                && value
                    .parse::<vonk_agent_protocol::generated::SecurityRefusalReason>()
                    .is_ok() =>
        {
            token.to_owned()
        }
        _ => "[redacted opaque value]".to_owned(),
    }
}

/// A diagnostic field name (`error_code`): a short identifier.
fn is_field_name(name: &str) -> bool {
    let mut characters = name.chars();
    name.len() <= 32
        && characters
            .next()
            .is_some_and(|first| first.is_ascii_alphabetic() || first == '_')
        && characters.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// The typed facts of a process-exit summary (`exit_code=1 exit_cause=...`):
/// fixed keys with token values, never the workload's exception after them,
/// which may itself read `exit_code=...`.
pub fn exit_facts(text: &str) -> impl Iterator<Item = (&str, &str)> {
    text.split(EXIT_SUMMARY_EXCEPTION_FIELD)
        .next()
        .unwrap_or_default()
        .split_whitespace()
        .filter_map(|token| token.split_once('='))
        .filter(|(key, value)| {
            use vonk_agent_protocol::host_memory_guard_policy::{
                PRESSURE_PROPERTY, RECENT_PROPERTY,
            };
            (matches!(
                *key,
                "exit_code"
                    | EXIT_CAUSE_FIELD
                    | vonk_agent_protocol::failure_evidence::OOM_KILLED_FIELD
                    | "reason"
                    | "trigger"
                    | "mem_available_bytes"
                    | "memory_full_avg10"
                    | "sustained_ms"
                    | "observed_at"
            ) || *key == PRESSURE_PROPERTY
                || *key == RECENT_PROPERTY)
                && value.chars().all(|c| {
                    c.is_ascii_alphanumeric()
                        || c == '_'
                        || c == '-'
                        || c == '.'
                        || c == ':'
                        || c == '+'
                })
        })
}

/// Sanitize a helper's process-exit summary.  The uncaught exception it ends
/// with is the workload's own text and is redacted as a line of its own: a
/// credential in that text then costs the exception, never the exit facts in
/// front of it.
pub fn sanitize_exit_summary(text: &str) -> String {
    match text.split_once(EXIT_SUMMARY_EXCEPTION_FIELD) {
        Some((facts, exception)) => format!(
            "{}{EXIT_SUMMARY_EXCEPTION_FIELD}{}",
            sanitize_text(facts),
            sanitize_text(exception)
        ),
        None => sanitize_text(text),
    }
}

/// Keep the end of `text` within `limit` bytes, preferring a line boundary, and
/// report how many bytes that dropped.
///
/// The bound is applied from the front because the end is what happened last.
/// Cutting the end instead would keep stale lines and discard the failure.
#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
fn clamp_tail(text: String, limit: usize) -> (String, usize) {
    if text.len() <= limit {
        return (text, 0);
    }
    let mut cut = text.len() - limit;
    while cut < text.len() && !text.is_char_boundary(cut) {
        cut += 1;
    }
    if let Some(index) = text[cut..].find('\n')
        && cut + index + 1 < text.len()
    {
        cut += index + 1;
    }
    (text[cut..].to_owned(), cut)
}

/// Keep the front of `text` within `limit` bytes, ending on a line boundary
/// when one fits.
#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
fn keep_front(text: &str, limit: usize) -> &str {
    if text.len() <= limit {
        return text;
    }
    let cut = text.floor_char_boundary(limit);
    match text[..cut].rfind('\n') {
        Some(index) => &text[..=index],
        None => &text[..cut],
    }
}

/// Bound a helper's retained text without losing its shape: the failure lines
/// it kept from above its window, the omission marker, and the end of the
/// window.  Text with no marker is one tail and keeps its end.
#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
fn clamp_retained(text: String, limit: usize) -> (String, usize) {
    if text.len() <= limit {
        return (text, 0);
    }
    let mut start = 0;
    let marker = text.split_inclusive('\n').find_map(|line| {
        let range = start..start + line.len();
        start = range.end;
        is_omission_marker(line.trim_end_matches('\n')).then_some(range)
    });
    let Some(marker) = marker else {
        return clamp_tail(text, limit);
    };
    let (causes, tail) = (&text[..marker.start], &text[marker.end..]);
    let budget = limit.saturating_sub(marker.len());
    // The causes the helper keeps (first error lines, a native abort, memory
    // figures) take at most half of its bound; the tail takes whatever they
    // leave.
    let causes = keep_front(causes, (budget / 2).max(budget.saturating_sub(tail.len())));
    let (tail, _) = clamp_tail(tail.to_owned(), budget - causes.len());
    let kept = format!("{causes}{}{tail}", &text[marker]);
    let dropped = text.len() - kept.len();
    (kept, dropped)
}

pub fn log_tail(bytes: &[u8]) -> FailureLogTail {
    // Find the last logical lines before decoding or clipping them. Bare CR is
    // also a progress-line separator; CRLF counts as one separator. Keep only
    // borrowed source slices here so old or oversized lines do not accumulate
    // while scanning the stream.
    let announced = bytes.starts_with(TRUNCATED_LOG_MARKER);
    let (mut line_start, mut offset, mut discard_partial_line) = if announced {
        let after_marker = TRUNCATED_LOG_MARKER.len();
        let delimiter_len = if bytes[after_marker..].starts_with(b"\r\n") {
            Some(2)
        } else if bytes[after_marker..].starts_with(b"\n")
            || bytes[after_marker..].starts_with(b"\r")
        {
            Some(1)
        } else {
            None
        };
        if let Some(delimiter_len) = delimiter_len {
            (
                after_marker + delimiter_len,
                after_marker + delimiter_len,
                true,
            )
        } else {
            // A marker with no line delimiter leaves no trustworthy source
            // boundary. Fail closed instead of treating the suffix as a line.
            (bytes.len(), bytes.len(), false)
        }
    } else {
        (0, 0, false)
    };
    let mut selected: VecDeque<(&[u8], usize)> = VecDeque::with_capacity(LOG_LINES);
    let mut total_lines = 0;
    while offset < bytes.len() {
        let separator_len = match bytes[offset] {
            b'\n' => Some(1),
            b'\r' => Some(if bytes.get(offset + 1) == Some(&b'\n') {
                2
            } else {
                1
            }),
            _ => None,
        };
        if let Some(separator_len) = separator_len {
            if discard_partial_line {
                discard_partial_line = false;
            } else {
                if selected.len() == LOG_LINES {
                    selected.pop_front();
                }
                selected.push_back((
                    &bytes[line_start..offset],
                    offset - line_start + separator_len,
                ));
            }
            total_lines += 1;
            offset += separator_len;
            line_start = offset;
        } else {
            offset += 1;
        }
    }
    if line_start < bytes.len() && !discard_partial_line {
        if selected.len() == LOG_LINES {
            selected.pop_front();
        }
        selected.push_back((&bytes[line_start..], bytes.len() - line_start));
        total_lines += 1;
    }

    let selected_bytes = selected.iter().map(|(_, source_bytes)| source_bytes).sum();
    let mut safe_lines = Vec::with_capacity(selected.len());
    let mut dropped_by_line = 0_usize;
    for (line, _) in &selected {
        // Redact each complete logical line before its byte window is clipped.
        // Otherwise an Authorization/token marker at the front of an oversized
        // line could be lost while leaving its credential-bearing suffix.
        let decoded = String::from_utf8_lossy(line);
        let safe = sanitize_text(&decoded);
        let (safe, dropped) = clamp_tail(safe, LOG_BYTES);
        dropped_by_line = dropped_by_line.saturating_add(dropped);
        safe_lines.push(safe);
    }
    let joined = safe_lines.join("\n");
    let (safe, dropped_by_join) = clamp_tail(joined, LOG_BYTES);
    // Per-line clips are omitted before joining; the joined clip removes bytes
    // from that already-bounded text, so both counts describe distinct drops.
    let dropped_by_bound = dropped_by_line.saturating_add(dropped_by_join);
    FailureLogTail {
        text: safe,
        truncated: bytes.len() > LOG_BYTES
            || total_lines > LOG_LINES
            || dropped_by_bound > 0
            || announced,
        dropped_bytes: (!announced).then_some(
            (bytes.len().saturating_sub(selected_bytes) as u64)
                .saturating_add(dropped_by_bound as u64),
        ),
        dropped_lines: (!announced).then_some(total_lines.saturating_sub(LOG_LINES) as u64),
    }
}

/// How Podman names the step a failed build ended at, as the closing line of its
/// stderr: `Error: building at STEP "<the whole RUN command>": <cause>`.
const PODMAN_STEP_ECHO: &str = "building at STEP ";
/// How Podman closes the echo of a failed RUN step; a heredoc's script may
/// itself contain `": `, so a RUN step ends here, not at the first `": `.
const PODMAN_RUN_CLOSE: &[u8] = b"\": while running runtime:";
/// The bytes of the echoed command kept on that line, so the `build_step`
/// property read from it holds at most about 164 bytes (with the ellipsis).
const ECHOED_STEP_BYTES: usize = 160;
/// The bytes of a Go crash header line kept ahead of the tail.
const CRASH_HEADER_BYTES: usize = 240;

/// Where the echoed step that starts `rest` ends: at a RUN step's close, or for
/// a step without one (COPY, ADD) at the first `": `. Every reader of the
/// echo (the compaction, `exit_properties`) ends the step by this rule.
fn echoed_step_end(rest: &[u8]) -> Option<usize> {
    let find = |needle: &[u8]| {
        rest.windows(needle.len())
            .position(|window| window == needle)
    };
    find(PODMAN_RUN_CLOSE).or_else(|| find(b"\": "))
}

/// The retained tail of a Podman build's stderr, taken after the command
/// Podman echoes on its closing `building at STEP "…": <cause>` line is bounded.
///
/// Podman repeats the whole step there, and a long RUN fills the retained tail
/// by itself: the step's own error lines printed above it were dropped and the
/// evidence was the echoed command. The bytes cut from the echo count as
/// dropped, so the tail's bounds stay truthful.
pub fn podman_stderr_tail(stream: &[u8]) -> FailureLogTail {
    let (compacted, cut) = compact_echoed_step(stream);
    let mut tail = log_tail(&compacted);
    // A Go crash dumps long goroutine traces after its `panic: ` header; the
    // header is the cause, so it stays ahead of the tail like the step cause.
    if let Some(header) = go_crash_header(stream)
        && !tail.text.contains(&header)
    {
        let (kept, dropped) = clamp_tail(tail.text, LOG_BYTES - header.len() - 1);
        tail.text = format!("{header}\n{kept}");
        tail.truncated = true;
        tail.dropped_bytes = tail
            .dropped_bytes
            .map(|known| known.saturating_add(dropped as u64));
    }
    if cut > 0 {
        tail.truncated = true;
        tail.dropped_bytes = tail
            .dropped_bytes
            .map(|known| known.saturating_add(cut as u64));
    }
    tail
}

/// The first line of a Go runtime crash (`panic: ` or `fatal error: `), redacted
/// and bounded.
#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
fn go_crash_header(stream: &[u8]) -> Option<String> {
    let line = stream
        .split(|&byte| byte == b'\n')
        .find(|line| line.starts_with(b"panic: ") || line.starts_with(b"fatal error: "))?;
    let safe = sanitize_text(&String::from_utf8_lossy(line));
    let end = safe.floor_char_boundary(CRASH_HEADER_BYTES);
    Some(safe[..end].trim_end().to_owned())
}

/// The stream with the echoed step's command bounded, and the bytes it cut.
///
/// The command ends by [`echoed_step_end`], the rule `exit_properties` reads
/// the step by: what follows is Podman's cause (a COPY names the missing source
/// there), and it is kept whole. The cut falls on a
/// word boundary so a redactable value is never split into an unredactable
/// remainder; a heredoc's line breaks become spaces so the step stays one line.
fn compact_echoed_step(stream: &[u8]) -> (std::borrow::Cow<'_, [u8]>, usize) {
    use std::borrow::Cow;
    let marker = format!("{PODMAN_STEP_ECHO}\"");
    let Some(start) = find_last(stream, marker.as_bytes()) else {
        return (Cow::Borrowed(stream), 0);
    };
    let command_start = start + marker.len();
    let Some(close) = echoed_step_end(&stream[command_start..]) else {
        return (Cow::Borrowed(stream), 0);
    };
    if close <= ECHOED_STEP_BYTES {
        return (Cow::Borrowed(stream), 0);
    }
    let command = &stream[command_start..command_start + close];
    let kept = command[..=ECHOED_STEP_BYTES]
        .iter()
        .rposition(u8::is_ascii_whitespace)
        .map_or(&command[..0], |cut| &command[..cut]);
    let ellipsis = " …".as_bytes();
    let mut compacted = Vec::with_capacity(stream.len() - close + kept.len() + ellipsis.len());
    compacted.extend_from_slice(&stream[..command_start]);
    compacted.extend(kept.iter().map(|&byte| match byte {
        b'\n' | b'\r' => b' ',
        byte => byte,
    }));
    compacted.extend_from_slice(ellipsis);
    compacted.extend_from_slice(&stream[command_start + close..]);
    (Cow::Owned(compacted), close - kept.len())
}

fn find_last(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .rposition(|window| window == needle)
}

/// Apply the current redaction to a tail the helper already bounded from the
/// whole stream, without re-selecting it.  The agent never sees that stream, so
/// it cannot choose a better window; it may only shrink the text to the bound,
/// keeping the cause lines the helper retained ahead of it, and report the
/// bytes it had to drop.
pub fn sanitize_tail(tail: &FailureLogTail) -> FailureLogTail {
    let safe = sanitize_text(&tail.text);
    let (safe, dropped) = clamp_retained(safe, LOG_BYTES);
    FailureLogTail {
        text: safe,
        truncated: tail.truncated || dropped > 0,
        dropped_bytes: tail
            .dropped_bytes
            .and_then(|known| known.checked_add(dropped as u64)),
        dropped_lines: tail.dropped_lines,
    }
}

/// The evidence a failed action reports: the rejected container's own output
/// when the helper read it, and otherwise the rejection's bounded detail.
///
/// A single detail string is the last resort, not the shape of the evidence:
/// merging a container's two streams into one tail is what discarded the
/// engine's own explanation of why a workload exited.
pub fn diagnostic_logs(
    process_logs: Option<&FailureProcessLogs>,
    diagnostic: Option<&str>,
) -> Option<FailureProcessLogs> {
    if let Some(logs) = process_logs {
        return Some(logs.clone());
    }
    diagnostic.map(detail_logs)
}

/// A single bounded detail string as the evidence of a failure that captured no
/// process output: the detail is the stderr tail, stdout is empty.
pub fn detail_logs(detail: &str) -> FailureProcessLogs {
    FailureProcessLogs {
        exit_code: None,
        stdout: log_tail(&[]),
        stderr: log_tail(detail.as_bytes()),
    }
}

pub fn category(code: &str) -> FailureCategory {
    let code = code.to_lowercase();
    for (markers, category) in [
        (
            &[
                "permission",
                "denied",
                "namespace",
                "sandbox",
                "policy",
                "mapping",
                "proc-mount",
            ][..],
            FailureCategory::PlatformPolicy,
        ),
        (
            &["space", "storage", "capacity", "memory-limit"][..],
            FailureCategory::Capacity,
        ),
        (
            &["digest", "integrity", "verification", "checksum"][..],
            FailureCategory::Digest,
        ),
        (&["timeout", "deadline"][..], FailureCategory::Timeout),
        (
            &[
                "network",
                "connection",
                "download",
                "upstream",
                "rate_limit",
            ][..],
            FailureCategory::Network,
        ),
    ] {
        if markers.iter().any(|marker| code.contains(marker)) {
            return category;
        }
    }
    FailureCategory::Runtime
}

fn bounded_file(path: &Path) -> std::io::Result<String> {
    let mut bytes = Vec::new();
    File::open(path)?.take(8192).read_to_end(&mut bytes)?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn bounded_command(program: &str, args: &[&str], deadline: Instant) -> std::io::Result<String> {
    if Instant::now() >= deadline {
        return Err(std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            "collection-budget-exhausted",
        ));
    }
    let mut child = Command::new(program)
        .args(args)
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LANG", "C")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(Stdio::piped())
        .spawn()?;
    let result = (|| {
        let mut output = child
            .stdout
            .take()
            .ok_or_else(|| std::io::Error::other("diagnostic-pipe-unavailable"))?;
        let flags = rustix::fs::fcntl_getfl(&output)?;
        rustix::fs::fcntl_setfl(&output, flags | rustix::fs::OFlags::NONBLOCK)?;
        let mut bytes = Vec::new();
        loop {
            let mut chunk = [0_u8; 1024];
            match output.read(&mut chunk) {
                Ok(0) => {
                    if let Some(status) = child.try_wait()? {
                        return if status.success() {
                            Ok(String::from_utf8_lossy(&bytes).into_owned())
                        } else {
                            Err(std::io::Error::other("diagnostic-command-failed"))
                        };
                    }
                }
                Ok(count) => {
                    if bytes.len() + count > 8192 {
                        return Err(std::io::Error::other("diagnostic-output-limit"));
                    }
                    bytes.extend_from_slice(&chunk[..count]);
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(error) => return Err(error),
            }
            if Instant::now() >= deadline {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "diagnostic-command-timeout",
                ));
            }
            std::thread::sleep(Duration::from_millis(1));
        }
    })();
    if result.is_err() {
        #[expect(
            clippy::let_underscore_must_use,
            reason = "best-effort stop of a process that may already have ended"
        )]
        let _ = child.kill();
        #[expect(
            clippy::let_underscore_must_use,
            reason = "best-effort stop of a process that may already have ended"
        )]
        let _ = child.wait();
    }
    result
}

fn systemd_properties(deadline: Instant) -> std::io::Result<Vec<FailureProperty>> {
    let text = bounded_command(
        "/usr/bin/systemctl",
        &[
            "show",
            "--no-pager",
            "--property=NoNewPrivileges,PrivateTmp,ProtectSystem,RestrictNamespaces,ReadWritePaths,ReadOnlyPaths,User",
            "vonk-forge-agent.service",
        ],
        deadline,
    )?;
    Ok(text
        .lines()
        .filter_map(|line| line.split_once('='))
        .take(8)
        .map(|(name, value)| FailureProperty {
            name: name.chars().take(64).collect(),
            value: sanitize_text(value).chars().take(256).collect(),
        })
        .collect())
}

pub fn collect(phase: &str, code: &str, stdout: &[u8], stderr: &[u8]) -> FailureDiagnostics {
    let start = Instant::now();
    let mut evidence = FailureDiagnostics {
        model_store_layout: None,
        page_cache: None,
        process_exit_code: None,
        schema_version: 1,
        collected_at: Utc::now().to_rfc3339(),
        phase: sanitize_text(phase).chars().take(80).collect(),
        category: category(code),
        stdout: log_tail(stdout),
        stderr: log_tail(stderr),
        versions: vec![FailureProperty {
            name: "agent".into(),
            value: env!("CARGO_PKG_VERSION").into(),
        }],
        sandbox: Vec::new(),
        storage: Vec::new(),
        preflight: Vec::new(),
        collector_errors: Vec::new(),
    };
    match bounded_file(Path::new("/proc/sys/kernel/osrelease")) {
        Ok(kernel) => evidence.versions.push(FailureProperty {
            name: "kernel".into(),
            value: kernel.trim().chars().take(256).collect(),
        }),
        Err(_) => evidence
            .collector_errors
            .push("kernel-observation-unavailable".into()),
    }
    match bounded_file(Path::new("/proc/self/status")) {
        Ok(status) => {
            for line in status.lines() {
                if let Some((key, value)) = line.split_once(':')
                    && ["NoNewPrivs", "Seccomp", "CapEff", "Uid", "Gid"].contains(&key)
                {
                    evidence.sandbox.push(FailureProperty {
                        name: key.into(),
                        value: value.trim().chars().take(256).collect(),
                    });
                }
            }
        }
        Err(_) => evidence
            .collector_errors
            .push("sandbox-observation-unavailable".into()),
    }
    // These are the effective kernel restrictions. A systemd configuration
    // value is not guessed from a unit template or a different service.
    match systemd_properties(start + Duration::from_millis(150)) {
        Ok(properties) => evidence.sandbox.extend(properties),
        Err(_) => evidence
            .collector_errors
            .push("systemd-properties-unavailable".into()),
    }
    evidence.sandbox.truncate(12);
    match bounded_command(
        "/usr/bin/podman",
        &["--version"],
        start + Duration::from_millis(200),
    ) {
        Ok(version) => evidence.versions.push(FailureProperty {
            name: "podman".into(),
            value: sanitize_text(version.trim()).chars().take(256).collect(),
        }),
        Err(_) => evidence
            .collector_errors
            .push("runtime-version-unavailable".into()),
    }
    if start.elapsed().as_millis() < 200 {
        match rustix::fs::statvfs("/var/lib/vonk-forge-agent") {
            Ok(stat) => {
                evidence.storage.push(FailureProperty {
                    name: "data-root".into(),
                    value: "/var/lib/vonk-forge-agent".into(),
                });
                evidence.storage.push(FailureProperty {
                    name: "free-bytes".into(),
                    value: stat.f_bavail.saturating_mul(stat.f_frsize).to_string(),
                });
            }
            Err(_) => evidence
                .collector_errors
                .push("storage-observation-unavailable".into()),
        }
    } else {
        evidence
            .collector_errors
            .push("collection-budget-exhausted".into());
    }
    evidence
}

/// The diagnostics of a stopped run whose inspection answered with the host
/// memory guard's receipt (its sanitized summary), or `None` for any other.
pub fn host_memory_receipt(summary: &str) -> Option<FailureDiagnostics> {
    (ExitCause::of_summary(summary) == Some(ExitCause::HostMemoryExhausted)).then(|| {
        from_failure(
            &AgentOperation::RecipeStart,
            &Failure::new(
                vonk_agent_protocol::generated::FailureCode::WorkloadHostMemoryExhausted.as_str(),
            )
            .diagnostic(summary)
            .exit_summary(Some(summary)),
        )
    })
}

pub fn from_failure(operation: &AgentOperation, failure: &Failure) -> FailureDiagnostics {
    let phase = failure
        .stage
        .map_or_else(|| operation.to_string(), |stage| stage.to_string());
    let code = [
        failure.code.map(|code| code.to_string()),
        failure.diagnostic.clone(),
        failure.helper_error_code.map(|code| code.to_string()),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>()
    .join(" ");
    let mut value = collect(&phase, &code, &[], &[]);
    value
        .model_store_layout
        .clone_from(&failure.model_store_layout);
    value.page_cache.clone_from(&failure.page_cache);
    if let Some(logs) = &failure.process_logs {
        value.process_exit_code = logs.exit_code;
        value.stdout = logs.stdout.clone();
        value.stderr = logs.stderr.clone();
    }
    if let Some(property) = failure.refusal_bound.as_ref().and_then(refusal_property) {
        value.preflight.push(property);
    }
    for property in exit_properties(failure) {
        if value.preflight.len() < vonk_agent_protocol::failure_evidence::MAX_PREFLIGHT_PROPERTIES {
            value.preflight.push(property);
        }
    }
    if matches!(
        failure
            .diagnostic
            .as_deref()
            .and_then(ExitCause::of_summary),
        Some(ExitCause::OomKilled | ExitCause::HostMemoryExhausted)
    ) {
        value.category = FailureCategory::Capacity;
    }
    value
}

/// The stable facts of how a process ended, as discrete properties: the exit
/// code and cause the helper reported for a container, and the failing step and
/// exit status of an image build (read from the build's own retained output).
/// Names and values are fixed tokens or bounded integers, never free text.
#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
fn exit_properties(failure: &Failure) -> Vec<FailureProperty> {
    let mut found = Vec::new();
    let mut push = |name: &str, value: String| {
        found.push(FailureProperty {
            name: name.into(),
            value: value.chars().take(256).collect(),
        });
    };
    // The helper's whole exit summary when the failure kept one: the wire
    // diagnostic is a bounded copy that can cut its facts.
    if let Some(text) = failure
        .exit_summary
        .as_deref()
        .or(failure.diagnostic.as_deref())
    {
        for (key, value) in exit_facts(text) {
            push(key, value.to_owned());
        }
    }
    if let Some(logs) = &failure.process_logs {
        let text = format!("{}\n{}", logs.stdout.text, logs.stderr.text);
        if let Some(step) = text
            .lines()
            .rev()
            .find_map(|line| line.split_once(PODMAN_STEP_ECHO).map(|(_, rest)| rest))
        {
            // `"RUN python3 x.py": while running runtime: exit status 1`
            let command = echoed_step_end(step.as_bytes())
                .map_or(step, |end| &step[..end])
                .trim_matches('"');
            push("build_step", command.to_owned());
            if let Some((_, status)) = step.rsplit_once("exit status ") {
                let digits: String = status.chars().take_while(char::is_ascii_digit).collect();
                if !digits.is_empty() {
                    push("exit_code", digits);
                }
            }
        }
    }
    found
}

/// The refusing rule and the measured bound of a refused request, when the
/// failure carries them. Only the stable rule code and two bounded integers
/// cross; the offending argument itself is never read.
fn refusal_property(bound: &RefusalBound) -> Option<FailureProperty> {
    let rule: String = bound
        .rule
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || *character == '_')
        .take(64)
        .collect();
    if rule.is_empty() {
        return None;
    }
    let observed = bound.observed;
    let value = match bound.limit {
        Some(limit) => format!("rule={rule} limit={limit} observed={observed}"),
        None => format!("rule={rule} observed={observed}"),
    };
    Some(FailureProperty {
        name: "request_refusal".into(),
        value: value.chars().take(256).collect(),
    })
}

#[cfg(test)]
mod tests;
