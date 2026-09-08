//! Bounded, sanitized observations captured locally after an operation fails.
//! Fixed local systemd query only; no network, environment dumps or directory scans.

use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    fs::File,
    io::Read,
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

const LOG_BYTES: usize = 2048;
const LOG_LINES: usize = 32;

pub use vonk_agent_protocol::failure_evidence::{
    FailureCategory, FailureDiagnostics, FailureLogTail, FailureProperty,
};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FailureProcessLogs {
    pub stdout: FailureLogTail,
    pub stderr: FailureLogTail,
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
            let lower = line.to_lowercase();
            if [
                "password",
                "secret",
                "token",
                "authorization",
                "cookie",
                "credential",
                "private key",
                "private_key",
                "api_key",
                "api-key",
                "-----begin",
                "-----end",
            ]
            .iter()
            .any(|marker| lower.contains(marker))
            {
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

pub fn log_tail(bytes: &[u8]) -> FailureLogTail {
    let start = bytes.len().saturating_sub(LOG_BYTES);
    let selected = &bytes[start..];
    let selected = if start > 0 {
        selected
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(&[][..], |index| &selected[index + 1..])
    } else {
        selected
    };
    let text = String::from_utf8_lossy(selected);
    let lines = text.lines().collect::<Vec<_>>();
    let total_lines = bytes.iter().filter(|b| **b == b'\n').count();
    let tail = lines[lines.len().saturating_sub(LOG_LINES)..].join("\n");
    let mut safe = sanitize_text(&tail);
    while safe.len() > LOG_BYTES {
        safe.pop();
    }
    FailureLogTail {
        text: safe,
        truncated: start > 0
            || lines.len() > LOG_LINES
            || bytes.starts_with(b"[earlier diagnostic output truncated]"),
        dropped_bytes: (!bytes.starts_with(b"[earlier diagnostic output truncated]"))
            .then_some((bytes.len() - selected.len()) as u64),
        dropped_lines: (!bytes.starts_with(b"[earlier diagnostic output truncated]"))
            .then_some(total_lines.saturating_sub(LOG_LINES) as u64),
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
    value
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
