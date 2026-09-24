//! Bounded, sanitized observations captured locally after an operation fails.
//! Fixed local systemd query only; no network, environment dumps or directory scans.

use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::VecDeque,
    fs::File,
    io::Read,
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

const LOG_BYTES: usize = 2048;
const LOG_LINES: usize = 32;
const TRUNCATED_LOG_MARKER: &[u8] = b"[earlier diagnostic output truncated]";

pub use vonk_agent_protocol::failure_evidence::{
    FailureCategory, FailureDiagnostics, FailureLogTail, FailureProperty,
};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FailureProcessLogs {
    pub stdout: FailureLogTail,
    pub stderr: FailureLogTail,
}

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
            line.split_whitespace()
                .map(|word| {
                    if let Ok(mut url) = url::Url::parse(word)
                        && matches!(url.scheme(), "http" | "https")
                    {
                        let _ = url.set_username("");
                        let _ = url.set_password(None);
                        url.set_query(None);
                        url.set_fragment(None);
                        return url.to_string();
                    }
                    if word.len() >= 40
                        && word
                            .chars()
                            .all(|c| c.is_ascii_alphanumeric() || "+/=_-".contains(c))
                    {
                        "[redacted opaque value]".to_owned()
                    } else {
                        word.to_owned()
                    }
                })
                .collect::<Vec<_>>()
                .join(" ")
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// Keep the end of `text` within `limit` bytes, preferring a line boundary, and
/// report how many bytes that dropped.
///
/// The bound is applied from the front because the end is what happened last.
/// Cutting the end instead would keep stale lines and discard the failure.
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
    for (line, _) in selected.iter() {
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

/// Apply the current redaction to a tail the helper already bounded from the
/// whole stream, without re-selecting it.  The agent never sees that stream, so
/// it cannot choose a better window; it may only shrink the text to the bound
/// and report the bytes it had to drop.
pub fn sanitize_tail(tail: &FailureLogTail) -> FailureLogTail {
    let safe = sanitize_text(&tail.text);
    let (safe, dropped) = clamp_tail(safe, LOG_BYTES);
    FailureLogTail {
        text: safe,
        truncated: tail.truncated || dropped > 0,
        dropped_bytes: Some(tail.dropped_bytes.unwrap_or_default() + dropped as u64),
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
) -> Option<Value> {
    if let Some(logs) = process_logs {
        return serde_json::to_value(logs).ok();
    }
    diagnostic.map(|detail| {
        serde_json::json!({
            "stdout": log_tail(&[]),
            "stderr": log_tail(detail.as_bytes()),
        })
    })
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
        let _ = child.kill();
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

pub fn from_failure(operation: &str, body: &Value) -> FailureDiagnostics {
    let phase = body
        .get("stage")
        .and_then(Value::as_str)
        .unwrap_or(operation);
    let code = ["error_code", "diagnostic", "helper_error_code"]
        .iter()
        .filter_map(|key| body.get(*key).and_then(Value::as_str))
        .collect::<Vec<_>>()
        .join(" ");
    let mut value = collect(phase, &code, &[], &[]);
    if let Some(logs) = body.get("diagnostic_logs").filter(|value| !value.is_null()) {
        if let Ok(logs) = serde_json::from_value::<FailureProcessLogs>(logs.clone()) {
            value.stdout = logs.stdout;
            value.stderr = logs.stderr;
        } else {
            value
                .collector_errors
                .push("process-log-evidence-invalid".into());
        }
    }
    if let Some(property) = refusal_property(body) {
        value.preflight.push(property);
    }
    value
}

/// The refusing rule and the measured bound of a refused request, when the
/// failure carries them. Only the stable rule code and two bounded integers
/// cross; the offending argument itself is never read from the body.
fn refusal_property(body: &Value) -> Option<FailureProperty> {
    let bound = body.get("refusal_bound")?.as_object()?;
    let rule = bound.get("rule")?.as_str()?;
    let observed = bound.get("observed")?.as_u64()?;
    let limit = bound.get("limit").and_then(Value::as_u64);
    let rule: String = rule
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || *character == '_')
        .take(64)
        .collect();
    if rule.is_empty() {
        return None;
    }
    let value = match limit {
        Some(limit) => format!("rule={rule} limit={limit} observed={observed}"),
        None => format!("rule={rule} observed={observed}"),
    };
    Some(FailureProperty {
        name: "request_refusal".into(),
        value: value.chars().take(256).collect(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn diagnostic_commands_have_hard_time_and_output_bounds() {
        let start = Instant::now();
        let error = bounded_command(
            "/bin/sh",
            &["-c", "while :; do :; done"],
            start + Duration::from_millis(20),
        )
        .unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::TimedOut);
        assert!(start.elapsed() < Duration::from_secs(1));
        let error = bounded_command(
            "/bin/sh",
            &["-c", "while :; do echo diagnostic; done"],
            Instant::now() + Duration::from_secs(1),
        )
        .unwrap_err();
        assert!(error.to_string().contains("output-limit"));
        assert_eq!(
            sanitize_text(
                "Auth\u{200b}orization: Bearer hidden\nsecret\0=hidden\npermission denied"
            ),
            "[redacted diagnostic line]\n[redacted diagnostic line]\npermission denied"
        );
    }

    #[test]
    fn a_configuration_line_that_names_tokens_is_not_redacted() {
        // Wrong implementation this catches: a bare ``token`` alternative in the
        // line filter replaced every line that mentioned
        // ``num_speculative_tokens`` with ``[redacted diagnostic line]``, which
        // removed the launch configuration a failed workload is diagnosed from
        // while redacting nothing that was secret.
        let configuration = "vllm: speculative-config {num_speculative_tokens: 7, method: dflash}";
        assert_eq!(sanitize_text(configuration), configuration);
        assert_eq!(
            sanitize_text("tokenizer_config.json was loaded without a token"),
            "tokenizer_config.json was loaded without a token"
        );
        // The credential value itself is still removed.
        assert_eq!(
            sanitize_text("HF_TOKEN=hunter2"),
            "[redacted diagnostic line]"
        );
        assert_eq!(
            sanitize_text("token: hunter2"),
            "[redacted diagnostic line]"
        );
    }

    #[test]
    fn a_bounded_tail_keeps_its_last_line_and_reports_the_drop() {
        // Wrong implementation this catches: the byte bound popped characters
        // off the end, so the text ended in a half-written word and the line
        // that reported the failure was the first thing discarded.
        let text = "noise\n".repeat(2000) + "the engine exited here\n";
        let (kept, dropped) = clamp_tail(text.clone(), LOG_BYTES);
        assert!(kept.ends_with("the engine exited here\n"), "{kept}");
        assert!(kept.len() <= LOG_BYTES);
        assert_eq!(dropped, text.len() - kept.len());
        assert!(dropped > 0);
    }

    #[test]
    fn an_oversized_single_line_keeps_its_trailing_failure() {
        // Wrong implementation this catches: selecting the last byte window
        // before finding a line boundary turns a long Podman error line into an
        // empty tail when its only newline is at the end.
        let text = format!(
            "{}permission denied while building image\n",
            "STEP echo ".repeat(500)
        );
        let tail = log_tail(text.as_bytes());
        assert!(tail.truncated);
        assert!(
            tail.text
                .ends_with("permission denied while building image"),
            "{}",
            tail.text
        );
        assert!(tail.text.len() <= LOG_BYTES);
    }

    #[test]
    fn an_oversized_carriage_return_progress_stream_keeps_its_final_failure() {
        // Podman progress may use bare carriage returns instead of linefeeds.
        // Wrong implementation: it treats the whole stream as one partial
        // line and discards the useful final status.
        let text = format!(
            "{}permission denied during image build",
            "copying layer 98%\r".repeat(300)
        );
        let tail = log_tail(text.as_bytes());
        assert!(tail.truncated);
        assert!(
            tail.text.ends_with("permission denied during image build"),
            "{}",
            tail.text
        );
        assert!(tail.text.len() <= LOG_BYTES);
    }

    #[test]
    fn an_oversized_credential_line_is_redacted_before_the_tail_is_clipped() {
        // Wrong implementation this catches: clipping before redaction drops
        // the Authorization marker and could expose a credential-bearing suffix.
        let credential = "sensitive-credential-value".repeat(150);
        let text = format!("Authorization: Bearer {credential} permission denied\n");
        let tail = log_tail(text.as_bytes());
        assert!(tail.truncated);
        assert_eq!(tail.text, "[redacted diagnostic line]");
        assert!(!tail.text.contains("sensitive-credential-value"));
    }

    #[test]
    fn an_oversized_utf8_line_keeps_a_valid_bounded_tail() {
        // The byte bound must not split a multibyte code point while keeping
        // the final diagnostic from a long line.
        let text = format!("{}permission denied", "🧱".repeat(700));
        let tail = log_tail(text.as_bytes());
        assert!(tail.truncated);
        assert!(tail.text.ends_with("permission denied"));
        assert!(tail.text.len() <= LOG_BYTES);
    }

    #[test]
    fn byte_drop_count_includes_clipped_earlier_lines() {
        // The per-line clips happen before the joined tail is bounded. Both
        // losses must be reflected, even when a later short line survives.
        let first = format!("{}first failure\n", "x ".repeat(1100));
        let second = format!("{}second failure\n", "y ".repeat(1100));
        let text = format!("{first}{second}last status");
        let tail = log_tail(text.as_bytes());
        assert_eq!(tail.text, "last status");
        assert_eq!(
            tail.dropped_bytes,
            Some((text.len() - tail.text.len()) as u64)
        );
    }

    #[test]
    fn announced_truncation_discards_the_partial_first_source_line() {
        // DiagnosticRing prepends this marker to an arbitrary byte suffix. If
        // capture began after an Authorization/Bearer marker, the first suffix
        // fragment cannot be safely redacted and must be discarded to its next
        // line boundary.
        let text = format!(
            "{}\n=hunter2\npermission denied",
            String::from_utf8_lossy(TRUNCATED_LOG_MARKER)
        );
        let tail = log_tail(text.as_bytes());
        assert_eq!(tail.text, "permission denied");
        assert!(tail.truncated);
        assert_eq!(tail.dropped_bytes, None);
        assert_eq!(tail.dropped_lines, None);

        // Without a boundary there is no trustworthy complete diagnostic line.
        let text = format!(
            "{}\n=hunter2",
            String::from_utf8_lossy(TRUNCATED_LOG_MARKER)
        );
        assert!(log_tail(text.as_bytes()).text.is_empty());
    }

    #[test]
    fn clamp_tail_keeps_an_oversized_final_line_ending_in_newline() {
        // Wrong implementation this catches: moving past the only newline in
        // the retained window drops the entire oversized final line.
        let text = format!("{}permission denied\n", "x".repeat(LOG_BYTES + 128));
        let (kept, dropped) = clamp_tail(text, LOG_BYTES);
        assert!(kept.ends_with("permission denied\n"), "{kept}");
        assert!(kept.len() <= LOG_BYTES);
        assert!(dropped > 0);
    }

    #[test]
    fn a_helper_tail_is_redacted_without_being_reselected() {
        // The helper chose the window from the whole stream; the engine's cause
        // sits at the front of it. Re-tailing here would discard that cause,
        // which is the defect this keeps out.
        let cause = "RuntimeError: engine core could not bind NCCL to the fabric\n";
        let tail = FailureLogTail {
            text: format!("{cause}{}", "frame\n".repeat(300)),
            truncated: true,
            dropped_bytes: Some(9000),
            dropped_lines: Some(300),
        };
        let sanitized = sanitize_tail(&tail);
        assert!(sanitized.text.contains(cause.trim()), "{}", sanitized.text);
        assert!(sanitized.text.len() <= LOG_BYTES);
        assert!(sanitized.truncated);
        assert!(sanitized.dropped_bytes.unwrap_or_default() >= 9000);
        assert_eq!(sanitized.dropped_lines, Some(300));
    }

    #[test]
    fn an_unread_container_log_is_not_reported_as_an_empty_one() {
        // Absence of evidence is reported as absence: a container log that could
        // not be read must not arrive as a tail with nothing in it.
        let logs = diagnostic_logs(None, Some("the container log command failed"));
        let value = logs.expect("a detail is evidence");
        assert_eq!(value["stdout"]["text"], "");
        assert!(
            value["stderr"]["text"]
                .as_str()
                .unwrap_or_default()
                .contains("the container log command failed")
        );
    }

    #[test]
    fn redaction_and_ring_keep_final_failure_without_credentials() {
        let text = format!(
            "{}\nAuthorization: Bearer sensitive\nhttps://user:pass@example.test/file?signature=hidden\npermission denied mounting proc\n",
            "noise\n".repeat(5000)
        );
        let tail = log_tail(text.as_bytes());
        assert!(tail.truncated);
        assert!(tail.text.contains("permission denied mounting proc"));
        for secret in ["sensitive", "user:pass", "signature", "hidden"] {
            assert!(!tail.text.contains(secret));
        }
        assert!(tail.text.len() <= LOG_BYTES);
    }
    #[test]
    fn every_failure_family_produces_bounded_diagnostics() {
        for operation in [
            "recipe.build.v1",
            "artifact.distribution.v1",
            "recipe.image.import.v1",
            "recipe.install",
            "recipe.start",
            "recipe.job.run.v1",
            "agent.upgrade.v1",
        ] {
            let result = from_failure(
                operation,
                &serde_json::json!({"diagnostic":"permission-denied", "stage":"prepare"}),
            );
            assert!(matches!(result.category, FailureCategory::PlatformPolicy));
            assert_eq!(result.phase, "prepare");
            assert!(serde_json::to_vec(&result).unwrap().len() < 8192);
        }
    }
}
