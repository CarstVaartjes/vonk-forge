#![cfg(test)]
use super::*;
use std::fmt::Write as _;

/// Scales the fixtures that are sized against the retained bound.
const SCALE: usize = LOG_BYTES / 2048;

#[test]
fn a_retained_tail_is_bounded_as_the_shared_clamp_vector_says() {
    // The Controller's bound of the same text is held to this vector too.
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/fixtures/log-tail-omission-v1.json"
    ))
    .expect("log-tail omission vector");
    for case in vector["clamps"].as_array().expect("clamp cases") {
        let text = case["text"].as_str().expect("text").to_owned();
        let limit = usize::try_from(case["limit"].as_u64().expect("limit")).unwrap();
        let (kept, dropped) = clamp_retained(text.clone(), limit);
        assert_eq!(kept, case["kept"].as_str().expect("kept"), "{text:?}");
        assert_eq!(
            dropped as u64,
            case["dropped_bytes"].as_u64().expect("dropped"),
            "{text:?}"
        );
    }
}

#[test]
fn a_podman_crash_header_stays_ahead_of_its_goroutine_dump() {
    // Wrong implementation: keep only the last lines, which are all stack
    // frames, so the evidence never names the crash.
    let mut stderr = String::from(
        "uv: downloading wheels\npanic: runtime error: index out of range\n\ngoroutine 61 [running]:\n",
    );
    for frame in 0..80 {
        write!(
            stderr,
            "netavark.frame{frame}(...)\n\t/go/src/f{frame}.go:1\n"
        )
        .unwrap();
    }
    let tail = podman_stderr_tail(stderr.as_bytes());
    assert!(
        tail.text
            .starts_with("panic: runtime error: index out of range\n")
    );
    assert!(tail.text.contains("frame79"));
    assert!(tail.text.len() <= LOG_BYTES);
    assert!(tail.truncated);
}
#[test]
fn a_failed_image_build_names_its_step_and_exit_status() {
    let failure = Failure::new("build failed").process_logs(Some(FailureProcessLogs {
            exit_code: None,
            stdout: log_tail(b""),
            stderr: log_tail(
                b"ray is not on PATH in the base image\nError: building at STEP \"RUN python3 /opt/vonk/verify-runtime.py\": while running runtime: exit status 1\n",
            ),
        }));
    let found = exit_properties(&failure);
    assert!(found.iter().any(
            |p| p.name == "build_step" && p.value == "RUN python3 /opt/vonk/verify-runtime.py"
        ));
    assert!(
        found
            .iter()
            .any(|p| p.name == "exit_code" && p.value == "1")
    );
}

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
        sanitize_text("Auth\u{200b}orization: Bearer hidden\nsecret\0=hidden\npermission denied"),
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
    let text = "noise\n".repeat(2000 * SCALE) + "the engine exited here\n";
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
        "STEP echo ".repeat(500 * SCALE)
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
    let credential = "sensitive-credential-value".repeat(150 * SCALE);
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
    let text = format!("{}permission denied", "🧱".repeat(700 * SCALE));
    let tail = log_tail(text.as_bytes());
    assert!(tail.truncated);
    assert!(tail.text.ends_with("permission denied"));
    assert!(tail.text.len() <= LOG_BYTES);
}

#[test]
fn byte_drop_count_includes_clipped_earlier_lines() {
    // The per-line clips happen before the joined tail is bounded. Both
    // losses must be reflected, even when a later short line survives.
    let first = format!("{}first failure\n", "x ".repeat(1100 * SCALE));
    let second = format!("{}second failure\n", "y ".repeat(1100 * SCALE));
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
fn sanitizing_a_helper_tail_preserves_unknown_and_overflowed_counts() {
    // A bounded helper observation can expand during secret redaction.
    // Neither an absent original count nor an overflowing sum is a known
    // physical count; reporting zero or u64::MAX would invent evidence.
    for prior in [None, Some(u64::MAX)] {
        let tail = FailureLogTail {
            text: "token=x\n".repeat(200 * SCALE),
            truncated: false,
            dropped_bytes: prior,
            dropped_lines: None,
        };
        let sanitized = sanitize_tail(&tail);
        assert!(sanitized.truncated);
        assert!(sanitized.text.len() <= LOG_BYTES);
        assert_eq!(sanitized.dropped_bytes, None);
        assert_eq!(sanitized.dropped_lines, None);
    }
}

#[test]
fn a_typed_field_survives_the_opaque_value_rule() {
    // Wrong implementation this catches: judging `name=value` as one
    // opaque word erased `error_code=helper_request_plan_binding_invalid`
    // (46 characters) from a Controller grant refusal, so the evidence
    // named no cause. Only a contract code survives behind a name; the
    // Controller applies the same cases.
    let vector: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/opaque-value-redaction-v1.json"
        ))
        .unwrap();
    for case in vector["cases"].as_array().unwrap() {
        let text = case["text"].as_str().unwrap();
        assert_eq!(
            sanitize_text(text),
            case["redacted"].as_str().unwrap(),
            "{text}"
        );
    }
    // Wrong implementation: keeping any short value behind a name leaked
    // `key=hf_...`, `sessionid=<hex>` and `PGPASSWORD=...` from workload
    // tails. Only a contract code survives, never a UUID.
    for case in vector["must_not_contain"].as_array().unwrap() {
        let text = case["text"].as_str().unwrap();
        let secret = case["secret"].as_str().unwrap();
        assert!(!sanitize_text(text).contains(secret), "{text}");
    }
}

#[test]
fn a_traceback_keeps_its_indentation_while_its_secrets_are_redacted() {
    // Wrong implementation this catches: rebuilding each line from its
    // words moved every frame of a traceback to the left margin, so the
    // evidence no longer showed which call raised and which line it ran.
    let digest = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08";
    let traceback = format!(
        "Traceback (most recent call last):\n  File \"/opt/engine/fetch.py\", line 88, in fetch\n    response = session.get(url,\theaders=headers)\n  File \"/opt/engine/session.py\", line 12, in get\n        Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.ZRrHA1JJJW8opsbCGfG_HACGpVUMN_a9IV7pAx_Zmeo\nPermissionError: blob  {digest} was refused\n"
    );
    assert_eq!(
        sanitize_text(&traceback),
        "Traceback (most recent call last):\n  File \"/opt/engine/fetch.py\", line 88, in fetch\n    response = session.get(url,\theaders=headers)\n  File \"/opt/engine/session.py\", line 12, in get\n[redacted diagnostic line]\nPermissionError: blob  [redacted opaque value] was refused"
    );
}

/// The helper's shape: failure lines kept from above its window, the
/// omission marker, then the window that ends the stream.
fn helper_retained(causes: &str, tail: &str) -> FailureLogTail {
    FailureLogTail {
        text: format!(
            "{causes}{}\n{tail}",
            vonk_agent_protocol::failure_evidence::omission_marker(41_234)
        ),
        truncated: true,
        dropped_bytes: Some(41_234),
        dropped_lines: Some(400),
    }
}

#[test]
fn a_retained_tail_that_grows_past_the_bound_keeps_its_cause_and_its_end() {
    // Wrong implementation this catches: redaction grows the helper's
    // text past the bound and the bound cuts from the front, which is
    // where the helper put the engine's own error.
    let causes = "(EngineCore pid=178) ERROR 10-15 11:33:50 [core.py:708] EngineCore failed to start.\n(EngineCore pid=178) ValueError: Gemma4 vision tower rejects max_num_batched_tokens=2048\n";
    let tail = format!(
        "{}(APIServer pid=55) RuntimeError: Engine core initialization failed. See root cause above.\n",
        "(APIServer pid=55)   File \"/usr/lib/vllm/engine.py\", line 9, in wait\ntoken: v\n"
            .repeat(21 * SCALE)
    );
    let retained = helper_retained(causes, &tail);
    assert!(retained.text.len() <= LOG_BYTES);
    assert!(sanitize_text(&retained.text).len() > LOG_BYTES);

    let sanitized = sanitize_tail(&retained);
    assert!(sanitized.text.starts_with(causes), "{}", sanitized.text);
    assert!(sanitized.text.contains("[41234 bytes omitted]\n"));
    assert!(
        sanitized
            .text
            .ends_with("RuntimeError: Engine core initialization failed. See root cause above."),
        "{}",
        sanitized.text
    );
    assert!(!sanitized.text.contains("token: v"));
    assert!(sanitized.text.len() <= LOG_BYTES);
    assert!(sanitized.truncated);
    assert!(sanitized.dropped_bytes.unwrap_or_default() > 41_234);
}

#[test]
fn oversized_causes_keep_their_first_lines_and_leave_room_for_the_end() {
    // The causes keep their front and the tail its end, each within its
    // share, so neither side of the marker crowds the other out.
    let causes = "(EngineCore pid=178) ERROR first failure\n".to_owned()
        + &"(EngineCore pid=178) ERROR repeated\n".repeat(60);
    let tail = "frame\n".repeat(300) + "the process exited";
    let sanitized = sanitize_tail(&helper_retained(&causes, &tail));
    assert!(
        sanitized
            .text
            .starts_with("(EngineCore pid=178) ERROR first failure\n")
    );
    assert!(sanitized.text.contains("bytes omitted]\nframe\n"));
    assert!(sanitized.text.ends_with("the process exited"));
    assert!(sanitized.text.len() <= LOG_BYTES);
}

#[test]
fn the_helpers_window_survives_the_agent_when_decoding_grows_it() {
    // End to end through the helper's own retention: invalid UTF-8 in the
    // traceback frames decodes to U+FFFD and grows the retained text past
    // the bound.  The engine error the summary points at must still
    // reach the Controller, and so must the summary that ended the stream.
    let mut stream = Vec::new();
    for index in 0..20 {
        stream.extend(
                format!("(EngineCore pid=178) INFO 10-15 11:33:{index:02} [gpu_model_runner.py:3120] Loading weights\n")
                    .into_bytes(),
            );
    }
    stream.extend(
        b"(EngineCore pid=178) ERROR 10-15 11:33:50 [core.py:708] EngineCore failed to start.\n",
    );
    stream.extend(b"(EngineCore pid=178) ValueError: Gemma4 vision tower rejects max_num_batched_tokens=2048\n");
    stream.extend(b"(APIServer pid=55) Traceback (most recent call last):\n");
    for index in 0..116 {
        stream.extend(
                format!("(APIServer pid=55)   File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/utils.py\", line {index}, in wait_for_engine_startup\n")
                    .into_bytes(),
            );
        stream.extend(b"(APIServer pid=55)     \xff\xfe\xfd\xfc\xff\xfe\xfd\xfc\n");
    }
    stream.extend(b"(APIServer pid=55) RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}\n");
    let retained = vonk_agent_helper::runtime_logs::retain(&stream);
    assert!(
        retained
            .text
            .contains("ValueError: Gemma4 vision tower rejects")
    );
    assert!(retained.text.len() > LOG_BYTES, "{}", retained.text.len());

    let sanitized = sanitize_tail(&retained);
    let failed = sanitized.text.find("EngineCore failed to start");
    let cause = sanitized
        .text
        .find("ValueError: Gemma4 vision tower rejects");
    assert!(failed.is_some() && failed < cause, "{}", sanitized.text);
    assert!(
        sanitized.text.ends_with("Failed core proc(s): {}"),
        "{}",
        sanitized.text
    );
    assert!(sanitized.text.len() <= LOG_BYTES);
    assert!(sanitized.truncated);
}

#[test]
fn an_unread_container_log_is_not_reported_as_an_empty_one() {
    // Absence of evidence is reported as absence: a container log that could
    // not be read must not arrive as a tail with nothing in it.
    let logs = diagnostic_logs(None, Some("the container log command failed"));
    let value = logs.expect("a detail is evidence");
    assert_eq!(value.stdout.text, "");
    assert!(
        value
            .stderr
            .text
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
        AgentOperation::RecipeBuildV1,
        AgentOperation::ArtifactDistributionV1,
        AgentOperation::RecipeInstall,
        AgentOperation::RecipeStart,
        AgentOperation::RecipeJobRunV1,
        AgentOperation::AgentUpgradeV1,
    ] {
        let result = from_failure(
            &operation,
            &Failure::new("failed")
                .diagnostic("permission-denied")
                .stage(vonk_agent_protocol::generated::FailureStage::RunStorage),
        );
        assert!(matches!(result.category, FailureCategory::PlatformPolicy));
        assert_eq!(result.phase, "run-storage");
        assert!(serde_json::to_vec(&result).unwrap().len() < 8192);
    }
}
