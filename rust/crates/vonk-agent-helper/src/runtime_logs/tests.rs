#![cfg(test)]
use super::*;
use std::fmt::Write as _;

/// Scales the fixtures that are sized against the retained bound.
const SCALE: usize = RETAINED_BYTES / 2048;

/// A traceback with no exception line, as a process killed mid-write leaves
/// it: the frames are the last thing written.
fn cut_off_traceback() -> Vec<u8> {
    let mut value = String::from("Traceback (most recent call last):\n");
    for index in 0..24 * SCALE {
        writeln!(value,
                "(APIServer pid=56) File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/async_llm.py\", line {}, in from_vllm_config",
                200 + index
            ).unwrap();
        value.push_str("(APIServer pid=56)     return cls(\n");
        value.push_str("(APIServer pid=56) ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n");
    }
    value.into_bytes()
}

fn preceded_by_cause() -> Vec<u8> {
    let mut stream =
        b"NCCL WARN Call to ibv_reg_mr failed\nERROR drafter checkpoint is incompatible\n".to_vec();
    stream.extend(cut_off_traceback());
    stream
}

#[test]
fn the_first_error_survives_a_tail_of_repeated_noise() {
    // Wrong implementation this catches: a plain window kept the newest
    // bytes, so the root error printed first was lost behind four repeated
    // tracebacks of the same failure.
    let mut stream = b"(rank0) ERROR rank 0 died: root cause here\n".to_vec();
    for _ in 0..4 {
        stream.extend(b"(APIServer pid=57) Traceback (most recent call last):\n");
        for index in 0..12 * SCALE {
            stream.extend(
                    format!("(APIServer pid=57)   File \"/opt/vllm/stage_{index:03}.py\", line {index}, in run\n").into_bytes(),
                );
        }
        stream.extend(b"(APIServer pid=57) RuntimeError: Orchestrator initialization failed\n");
    }
    let tail = retain(&stream);
    assert!(
        tail.text
            .starts_with("(rank0) ERROR rank 0 died: root cause here\n")
    );
    assert!(tail.text.contains("bytes omitted]"));
    assert!(tail.text.len() <= RETAINED_BYTES, "{}", tail.text.len());
    assert!(
        tail.text
            .ends_with("RuntimeError: Orchestrator initialization failed\n")
    );
    assert_eq!(
        tail.dropped_bytes,
        Some((stream.len() - kept_bytes(&tail)) as u64)
    );
}

fn kept_bytes(tail: &FailureLogTail) -> usize {
    tail.text.len() - omission_marker(tail.dropped_bytes.unwrap()).len() - 1
}

#[test]
fn a_rank_one_traceback_above_the_rank_zero_pointer_is_retained_whole() {
    // Wrong implementation this catches: a 2 KiB bound kept the pointer and
    // the tail of the traceback it points at, so the first frames of the
    // rank-1 failure (about 3 KB above "See root cause above") were lost.
    let mut stream = b"(EngineCore pid=401) ERROR Traceback (most recent call last):\n".to_vec();
    for index in 0..26 {
        stream.extend(
                format!("(EngineCore pid=401)   File \"/opt/vllm/rank1/stage_{index:02}.py\", line {index}, in run_stage\n(EngineCore pid=401)     stage_{index:02}.run()\n").into_bytes(),
            );
    }
    stream.extend(b"(EngineCore pid=401) ValueError: rank 1 ran out of memory\n");
    stream.extend(b"(APIServer pid=55) RuntimeError: Engine core initialization failed. See root cause above.\n");
    assert!((3000..4000).contains(&stream.len()), "{}", stream.len());
    let tail = retain(&stream);
    assert!(tail.text.contains("rank1/stage_00.py"), "{}", tail.text);
    assert!(tail.text.contains("ValueError: rank 1 ran out of memory"));
    assert!(tail.text.ends_with("See root cause above.\n"));
    assert!(tail.text.len() > 2048 && tail.text.len() <= RETAINED_BYTES);
    assert!(!tail.truncated);
}

#[test]
fn a_short_stream_is_reported_whole() {
    let stream = b"first line\nsecond line\nthird line\n";
    let tail = retain(stream);
    assert_eq!(tail.text, "first line\nsecond line\nthird line\n");
    assert_eq!(tail.dropped_bytes, Some(0));
    assert!(!tail.truncated);
}

#[test]
fn a_cut_off_traceback_keeps_the_cause_above_it() {
    let tail = retain(&preceded_by_cause());
    assert!(tail.text.contains("drafter checkpoint is incompatible"));
    assert!(tail.text.contains("Traceback (most recent call last)"));
    assert!(tail.truncated);
    assert!(tail.text.len() <= RETAINED_BYTES);
}

#[test]
fn a_plain_tail_hides_the_cause_it_replaces() {
    // The regression this rule exists for: without the anchor the same
    // capture loses the cause and keeps frames only.
    let stream = preceded_by_cause();
    let tail = retain(&stream);
    let plain = String::from_utf8_lossy(&stream[stream.len() - RETAINED_BYTES..]);
    assert!(!plain.contains("drafter checkpoint is incompatible"));
    assert!(tail.text.contains("drafter checkpoint is incompatible"));
}

/// The live shape of the LTX job that aborted on spark-2297 (2026-11-05):
/// the pipeline's own log, `PyTorch`'s native abort report, its C++ stack,
/// then the job adapter's traceback for the child that died, whose
/// closing line echoes the whole command.
const FINAL_FRAMES: usize = 60 * SCALE;

fn native_abort_under_a_wrapper(wrapped: bool) -> Vec<u8> {
    let mut stream = String::new();
    for step in 0..80 {
        writeln!(
            stream,
            "INFO:ltx_pipelines:loading transformer block {step} of 80 from the checkpoint"
        )
        .unwrap();
    }
    stream.push_str("terminate called after throwing an instance of 'c10::Error'\n");
    stream.push_str("  what():  CUDA error: an illegal memory access was encountered\n");
    stream.push_str("CUDA kernel errors might be asynchronously reported at some other API call, so the stacktrace below might be incorrect.\n");
    stream.push_str("For debugging consider passing CUDA_LAUNCH_BLOCKING=1\n\n");
    stream.push_str("Exception raised from c10_cuda_check_implementation at /pytorch/c10/cuda/CUDAException.cpp:44 (most recent call first):\n");
    for frame in 0..9 {
        writeln!(stream,
                "frame #{frame}: c10::Error::Error(c10::SourceLocation, std::string) + 0x{frame}c (0xe8472ad284c4 in /usr/local/lib/python3.12/dist-packages/torch/lib/libc10.so)"
            ).unwrap();
    }
    for frame in 9..FINAL_FRAMES {
        writeln!(stream, "frame #{frame}: python3() [0x4f9e4c]").unwrap();
    }
    if wrapped {
        stream.push_str("\nTraceback (most recent call last):\n");
        stream.push_str(
            "  File \"/opt/vonk/source/pipelines/run.py\", line 267, in <module>\n    main()\n",
        );
        stream.push_str("  File \"/opt/vonk/source/pipelines/run.py\", line 259, in main\n");
        stream.push_str("    subprocess.run(command, check=True, timeout=args.timeout_seconds)\n");
        writeln!(stream,
                "subprocess.CalledProcessError: Command '['python3', '-m', 'ltx_pipelines.ti2vid_two_stages', {}]' died with <Signals.SIGABRT: 6>.",
                "'--option', 'value', ".repeat(40)
            ).unwrap();
    }
    stream.into_bytes()
}

#[test]
fn a_native_abort_keeps_its_message_above_the_wrappers_traceback() {
    // Wrong implementation this catches: the window ended on the job
    // adapter's traceback and spent its whole budget on that and the C++
    // frames above it, so the evidence of the LTX abort dropped the
    // `what()` message that said why the process aborted.
    let stream = native_abort_under_a_wrapper(true);
    let plain = String::from_utf8_lossy(&stream[stream.len() - RETAINED_BYTES..]);
    assert!(!plain.contains("what():"));
    let tail = retain(&stream);
    let text = &tail.text;
    let opener = text
        .find("terminate called after throwing an instance of 'c10::Error'")
        .expect(text);
    let message = text
        .find("  what():  CUDA error: an illegal memory access was encountered\n")
        .expect(text);
    let marker = text.find(" bytes omitted]\n").expect(text);
    assert!(opener < message && message < marker, "{text}");
    assert_eq!(text.matches(" bytes omitted]").count(), 1, "{text}");
    assert!(text.contains("For debugging consider passing CUDA_LAUNCH_BLOCKING=1\n"));
    assert!(!text.contains("loading transformer block"), "{text}");
    assert!(
        text.ends_with("died with <Signals.SIGABRT: 6>.\n"),
        "{text}"
    );
    assert!(text.len() <= RETAINED_BYTES, "{}", text.len());
    let dropped = tail.dropped_bytes.expect("the helper counts what it drops");
    assert!(dropped > 0 && dropped < stream.len() as u64);
}

#[test]
fn a_native_abort_with_no_traceback_keeps_its_message_and_its_last_frames() {
    // Wrong implementation this catches: with no Python header in the
    // stream the window was a plain tail of C++ frames.
    let stream = native_abort_under_a_wrapper(false);
    let tail = retain(&stream);
    let text = &tail.text;
    assert!(
        text.starts_with("terminate called after throwing"),
        "{text}"
    );
    assert!(text.contains("  what():  CUDA error: an illegal memory access"));
    assert!(
        text.ends_with(&format!(
            "frame #{}: python3() [0x4f9e4c]\n",
            FINAL_FRAMES - 1
        )),
        "{text}"
    );
    assert!(text.len() <= RETAINED_BYTES, "{}", text.len());
}

#[test]
fn a_native_abort_inside_the_window_is_not_repeated() {
    let mut stream = b"terminate called after throwing an instance of 'std::runtime_error'\n  what():  bad shape\n".to_vec();
    stream.extend_from_slice(b"frame #0: f()\n");
    let tail = retain(&stream);
    assert_eq!(tail.text, String::from_utf8_lossy(&stream));
    assert_eq!(tail.dropped_bytes, Some(0));
}

#[test]
#[expect(
    clippy::format_collect,
    reason = "builds a small string; a fold would read worse"
)]
fn a_cause_line_the_window_opens_inside_is_kept_whole() {
    // Wrong implementation this catches: a cause line was kept only when
    // it ended before the window's first byte, and the window drops the
    // partial line it opens inside, so a `what()` line straddling that
    // byte was in neither and the message was lost.
    let opener = "terminate called after throwing an instance of 'c10::Error'\n";
    let message = format!("  what():  CUDA error: {}\n", "e".repeat(176));
    assert_eq!(message.len(), 200);
    // The window keeps the bound less the causes and the marker reserve,
    // which opens inside the message line: the frames fill all of it but
    // half a message (every frame line is 33 bytes).
    let window = RETAINED_BYTES - opener.len() - message.len() - OMISSION_MARKER_BYTES;
    let count = (window - message.len() / 2) / 33;
    let frames: String = (0..count)
        .map(|frame| format!("frame #{frame:03}: python3() [0x4f9e4c]\n"))
        .collect();
    assert!(frames.len() < window && window < frames.len() + message.len());
    let stream = format!("{}{opener}{message}{frames}", "INFO loading\n".repeat(40));
    let tail = retain(stream.as_bytes());
    assert!(
        tail.text.starts_with(&format!("{opener}{message}[")),
        "{}",
        tail.text
    );
    assert_eq!(tail.text.matches("what():").count(), 1, "{}", tail.text);
    assert!(
        tail.text
            .ends_with(&format!("frame #{:03}: python3() [0x4f9e4c]\n", count - 1))
    );
    assert!(tail.text.len() <= RETAINED_BYTES, "{}", tail.text.len());
}

#[test]
#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
fn a_terminate_without_an_exception_is_one_line() {
    // Wrong implementation this catches: every line after any `terminate
    // called` was read as its message, so a report with no message took
    // up to half the budget from the window.
    let stream = format!(
        "terminate called without an active exception\n{}{}",
        "worker 3 still holds the device\n".repeat(20),
        "frame\n".repeat(400 * SCALE)
    );
    let tail = retain(stream.as_bytes());
    let marker = tail.text.find(" bytes omitted]\n").expect(&tail.text);
    assert_eq!(
        &tail.text[..tail.text[..marker].rfind('\n').map_or(0, |index| index + 1)],
        "terminate called without an active exception\n",
        "{}",
        tail.text
    );
    assert!(tail.text.len() <= RETAINED_BYTES, "{}", tail.text.len());
}

/// The live vLLM shape of a start that runs out of memory: the engine
/// logs what it loaded and what was left for the KV cache, then a
/// traceback longer than the bound that ends on vLLM's `ValueError`, and the
/// API server's traceback pointing above itself.
fn kv_cache_exhausted_start(frames: usize) -> Vec<u8> {
    let prefix = "(EngineCore pid=255) INFO 10-27 06:53:40 [gpu_worker.py:462]";
    let mut stream = b"Downloading apache-tvm-ffi (16.7 MB)\n".to_vec();
    for index in 0..100 {
        stream.extend(format!("{prefix} non-default args: max_model_len={index}\n").into_bytes());
    }
    stream.extend(
        format!("{prefix} Model loading took 64.98 GiB memory and 247.1 seconds\n").into_bytes(),
    );
    stream.extend(format!("{prefix} Estimated CUDA graph memory: 0.19 GiB total\n").into_bytes());
    stream.extend(format!("{prefix} Available KV cache memory: -1.25 GiB\n").into_bytes());
    let error = "(EngineCore pid=255) ERROR 10-27 06:53:52 [core.py:1231]";
    stream.extend(
        format!(
            "{error} EngineCore failed to start.\n{error} Traceback (most recent call last):\n"
        )
        .into_bytes(),
    );
    for index in 0..frames {
        stream.extend(
                format!("{error}   File \"/opt/vllm/v1/core/kv_cache_utils.py\", line {index}, in get_kv_cache_configs\n{error}     _check_enough_kv_cache_memory(\n").into_bytes(),
            );
    }
    stream.extend(format!("{error} ValueError: No available memory for the cache blocks. Try increasing `gpu_memory_utilization` when initializing the engine.\n").into_bytes());
    stream.extend(b"(APIServer pid=55) Traceback (most recent call last):\n");
    for index in 0..10 {
        stream.extend(
                format!("(APIServer pid=55)   File \"/opt/vllm/v1/engine/utils.py\", line {index}, in wait_for_engine_startup\n").into_bytes(),
            );
    }
    stream.extend(b"(APIServer pid=55) RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}\n");
    stream
}

#[test]
fn a_traceback_longer_than_the_bound_keeps_the_memory_figures_above_it() {
    // Wrong implementation this catches: the window kept only the last
    // frames and the exception, so every "No available memory for the
    // cache blocks" failure lost the figures needed to size its fix.
    let stream = kv_cache_exhausted_start(30 * SCALE);
    let plain = String::from_utf8_lossy(&stream[stream.len() - RETAINED_BYTES..]);
    assert!(!plain.contains("Available KV cache memory"));
    let tail = retain(&stream);
    assert!(tail.text.len() <= RETAINED_BYTES, "{}", tail.text);
    let lines: Vec<&str> = tail.text.lines().collect();
    let line_of = |needle: &str| lines.iter().position(|line| line.contains(needle));
    let loaded = line_of("Model loading took 64.98 GiB memory").expect(&tail.text);
    let graphs = line_of("Estimated CUDA graph memory: 0.19 GiB").expect(&tail.text);
    let kv = line_of("Available KV cache memory: -1.25 GiB").expect(&tail.text);
    let marker = lines
        .iter()
        .position(|line| vonk_agent_protocol::failure_evidence::is_omission_marker(line))
        .expect(&tail.text);
    let exception =
        line_of("ValueError: No available memory for the cache blocks").expect(&tail.text);
    assert!(loaded < graphs && graphs < kv && kv < marker && marker < exception);
    // A size that is not memory is not a figure.
    assert!(!tail.text.contains("Downloading apache-tvm-ffi"));
    // The counts stay truthful: what is shown plus what was dropped is the
    // whole stream.
    let shown = tail.text.len() - lines[marker].len() - 1;
    assert_eq!(tail.dropped_bytes, Some((stream.len() - shown) as u64));
    assert!(tail.truncated);
}

#[test]
fn a_memory_figure_at_the_top_of_the_window_is_not_lost_to_the_figures_above_it() {
    // Wrong implementation this catches: the figures were taken from above
    // the full window and their bytes cut from its top, so "Available KV
    // cache memory", which a shorter traceback leaves inside the window,
    // was cut out to make room for "Model loading took" above it.
    let stream = kv_cache_exhausted_start(7);
    let tail = retain(&stream);
    assert!(tail.text.len() <= RETAINED_BYTES, "{}", tail.text);
    for kept in [
        "Model loading took 64.98 GiB memory",
        "Available KV cache memory: -1.25 GiB",
        "ValueError: No available memory for the cache blocks",
    ] {
        assert!(tail.text.contains(kept), "{kept}: {}", tail.text);
    }
}

#[test]
fn a_memory_figure_is_kept_wherever_the_window_opens() {
    // Wrong implementation this catches: a figure line the smaller window
    // opened inside was neither a figure (it did not end above the window)
    // nor in the tail (a partial first line is dropped), so at some offsets
    // "Available KV cache memory" was lost although the full window held it.
    let stream = kv_cache_exhausted_start(4);
    let at = stream
        .windows(b"EngineCore failed to start".len())
        .position(|window| window == b"EngineCore failed to start")
        .unwrap();
    // Every figure line is longer than the step, so each one is opened
    // inside by both windows at some step.
    for pad in (0..RETAINED_BYTES).step_by(16) {
        let mut padded = stream[..at].to_vec();
        padded.extend(vec![b'x'; pad]);
        padded.extend(&stream[at..]);
        let tail = retain(&padded);
        assert!(tail.text.len() <= RETAINED_BYTES);
        for kept in [
            "Model loading took 64.98 GiB memory",
            "Estimated CUDA graph memory: 0.19 GiB",
            "Available KV cache memory: -1.25 GiB",
            "ValueError: No available memory for the cache blocks",
        ] {
            assert!(tail.text.contains(kept), "{pad} {kept}: {}", tail.text);
        }
    }
}

#[test]
fn memory_figures_above_a_traceback_never_cost_its_exception() {
    // Wrong implementation this catches: moving the window's start down
    // after it was placed cut the frame that raised the exception, and then
    // the exception itself, so the exit summary lost it and the exit was
    // unclassified.
    let mut stream = sglang_backend_refusal();
    stream.extend(b"[2026-10-15 08:12:01] Received sigquit from a child process. It usually means the child failed.\n".repeat(28));
    let state = exit(1, false, "");
    for figures in 0..3 {
        let mut stderr =
            b"[2026-10-15 08:11:58] Load weight end. avail mem=50.00 GB, mem usage=64.98 GB.\n"
                .repeat(figures);
        stderr.extend(&stream);
        let capture = CapturedOutput::new(b"", &stderr);
        let output = capture.retained();
        assert!(output.stderr.text.len() <= RETAINED_BYTES);
        assert!(
            python_exception(output).is_some_and(|text| text.contains("AssertionError")),
            "{figures}: {}",
            output.stderr.text
        );
        assert_eq!(
            classify(Some(&state), Some(&capture)),
            ExitCause::PythonException,
            "{figures}: {}",
            output.stderr.text
        );
    }
}

#[test]
fn a_traceback_ending_in_an_unlisted_exception_keeps_that_exception() {
    // Wrong implementation: the window ended on the `Traceback` opener
    // whenever the closing exception's class was not in the header list, so
    // a crashed vLLM start reported one line and hid why it exited.
    let mut stream = b"(APIServer pid=54) INFO starting\n".to_vec();
    stream.extend(b"(APIServer pid=54) Traceback (most recent call last):\n");
    for index in 0..24 {
        stream.extend(
                format!(
                    "(APIServer pid=54)   File \"/usr/lib/vllm/engine.py\", line {index}, in run\n(APIServer pid=54)     return cls(\n(APIServer pid=54)            ^^^^^^^^\n"
                )
                .into_bytes(),
            );
    }
    stream.extend(
        b"(APIServer pid=54) torch.OutOfMemoryError: CUDA out of memory while loading weights\n",
    );
    stream.extend(b"(APIServer pid=54) INFO shutting down\n");
    let tail = retain(&stream);
    assert!(
        tail.text.contains("OutOfMemoryError: CUDA out of memory"),
        "{}",
        tail.text
    );
    assert!(tail.text.len() <= RETAINED_BYTES);
}

#[test]
fn a_completed_traceback_keeps_its_exception() {
    let mut stream = preceded_by_cause();
    stream.extend(b"RuntimeError: Engine core initialization failed. See root cause above.\n");
    let tail = retain(&stream);
    assert!(tail.text.contains("See root cause above"));
    // The summary points above itself and nothing above it is a traceback:
    // the error line it points at is retained too.
    assert!(
        tail.text.contains("drafter checkpoint is incompatible"),
        "{}",
        tail.text
    );
    assert!(tail.text.len() <= RETAINED_BYTES);
}

/// The live vLLM shape: the engine process reports its failure as log
/// lines, not a traceback, and the API server then prints a long traceback
/// that ends by pointing above itself.
fn engine_error_then_api_server_traceback() -> Vec<u8> {
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
                format!(
                    "(APIServer pid=55)   File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/utils.py\", line {index}, in wait_for_engine_startup\n(APIServer pid=55)     return launch_core_engines(\n(APIServer pid=55)            ^^^^^^^^^^^^^^^^^^^^\n"
                )
                .into_bytes(),
            );
    }
    stream.extend(b"(APIServer pid=55) RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}\n");
    stream
}

#[test]
#[expect(
    clippy::naive_bytecount,
    reason = "small log streams; a counting dependency is not warranted"
)]
fn a_summary_pointing_above_itself_keeps_the_engine_error_printed_first() {
    // Wrong implementation this catches: with no traceback above the API
    // server's summary, the window kept only its last frames, and the
    // engine's own error -- printed first -- was always dropped.
    let stream = engine_error_then_api_server_traceback();
    assert!(stream.iter().filter(|byte| **byte == b'\n').count() > 350);
    let tail = retain(&stream);
    let engine = tail
        .text
        .find("ValueError: Gemma4 vision tower rejects")
        .expect(&tail.text);
    let failed = tail
        .text
        .find("EngineCore failed to start")
        .expect(&tail.text);
    let summary = tail
        .text
        .find("Engine core initialization failed. See root cause above.")
        .expect(&tail.text);
    assert!(failed < engine && engine < summary, "{}", tail.text);
    assert!(
        tail.text.ends_with("Failed core proc(s): {}\n"),
        "{}",
        tail.text
    );
    assert!(!tail.text.contains("Loading weights"), "{}", tail.text);
    assert!(tail.text.len() <= RETAINED_BYTES, "{}", tail.text.len());
    assert!(tail.truncated);
    let dropped = tail.dropped_bytes.expect("the helper counts what it drops");
    assert!(dropped > 0 && dropped < stream.len() as u64);
}

#[test]
fn an_undecodable_byte_cannot_move_the_anchor_out_of_the_stream() {
    // Wrong implementation this catches: the header offset was measured in
    // decoded characters, so a byte that is not valid UTF-8 made every
    // earlier line count for more than it occupies. The anchor then named a
    // position past the end of the capture -- a wrong window at best, and a
    // slice panic at worst -- exactly when a crashed process is most likely
    // to have written one.
    let mut stream = b"ERROR drafter checkpoint is incompatible\n".to_vec();
    stream.extend_from_slice(&[0xff, 0xfe, 0x00, 0x80]);
    stream.extend_from_slice(b"\nTraceback (most recent call last):\n");
    stream.extend(cut_off_traceback());
    let (_, end, _, _) = last_failure_line(&stream).expect("the capture has a header");
    assert!(end <= stream.len(), "{end} > {}", stream.len());
    let tail = retain(&stream);
    assert!(!tail.text.is_empty(), "{:?}", tail.text);
    // The declared bound is characters. An undecodable byte becomes one
    // replacement character, so the window can never decode into more
    // characters than the bytes it selected; only its byte weight grows.
    assert!(tail.text.chars().count() <= RETAINED_BYTES);
    assert!(tail.text.len() <= RETAINED_BYTES * 3);
}

#[test]
fn an_anchor_in_the_last_bytes_still_spends_the_whole_budget() {
    // Wrong implementation this catches: the window ran forward from the
    // anchor, so a process that died mid-write -- leaving the anchor in its
    // final line -- retained only the handful of bytes after it. The first
    // live GLM 5.3 Flash observation arrived as 1024 characters of an
    // EngineCore traceback for exactly this reason.
    let mut stream = b"zz\n".repeat(1000 * SCALE);
    stream.extend_from_slice(b"ERROR engine core died\n");
    let tail = retain(&stream);
    assert!(
        tail.text.contains("ERROR engine core died"),
        "{}",
        tail.text
    );
    // The whole budget, less at most the one partial line the window had to
    // open on, and it ends where the stream ended.
    assert!(RETAINED_BYTES - tail.text.len() < 8, "{}", tail.text.len());
    assert!(
        tail.text.ends_with("ERROR engine core died\n"),
        "{}",
        tail.text
    );
    assert!(tail.truncated);
    // Everything the window did not keep is reported, including the
    // partial line it opened on.
    assert_eq!(
        tail.dropped_bytes,
        Some((stream.len() - tail.text.len()) as u64)
    );
    assert!(tail.dropped_bytes.unwrap_or_default() >= (stream.len() - RETAINED_BYTES) as u64);
}

/// The live shape: a worker traceback, then the executor's summary that
/// points above itself.
fn worker_then_summary() -> Vec<u8> {
    let mut stream = b"(WorkerProc pid=200) Traceback (most recent call last):\n".to_vec();
    for frame in 0..8 {
        stream.extend_from_slice(
                format!(
                    "(WorkerProc pid=200)   File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/worker/gpu_worker.py\", line {}, in init_device\n",
                    300 + frame
                )
                .as_bytes(),
            );
    }
    stream.extend_from_slice(
        b"(WorkerProc pid=200) RuntimeError: NCCL error in: ncclAllReduce, unhandled cuda error\n",
    );
    stream.extend_from_slice(
            b"(EngineCore pid=177) ERROR 09-18 04:30:01 [core.py:1355] Traceback (most recent call last):\n",
        );
    for frame in 0..8 {
        stream.extend_from_slice(
                format!(
                    "(EngineCore pid=177) ERROR 09-18 04:30:01 [core.py:1355]   File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/executor/multiproc_executor.py\", line {}, in _init_executor\n",
                    210 + frame
                )
                .as_bytes(),
            );
    }
    stream.extend_from_slice(
            b"(EngineCore pid=177) ERROR 09-18 04:30:01 [core.py:1355] Exception: WorkerProc initialization failed due to an exception in a background process. See stack trace for root cause.\n",
        );
    stream
}

#[test]
fn a_block_that_points_above_itself_yields_to_the_cause_it_reports() {
    // Wrong implementation this catches: ending the window at the last
    // failure line keeps the summary's own frames and the sentence "See
    // stack trace for root cause.", spending the whole budget on a pointer
    // while the worker's RuntimeError is one block above it.
    let tail = retain(&worker_then_summary());
    assert!(tail.text.contains("NCCL error"), "{}", tail.text);
    assert!(
        !tail.text.contains("See stack trace for root cause"),
        "{}",
        tail.text
    );
    assert!(tail.text.len() <= RETAINED_BYTES);
}

/// The production shape of 2026-10-25 (operation f16a8f17, a dual-Spark
/// vLLM v0.27.0 start): a worker dies in a Triton kernel and the engine
/// core reports it in a block that closes "please check the stack trace
/// above for the root cause".  From `_initialize_kv_caches` down these are
/// the captured lines; the three frames above it were not retained and
/// carry v0.27.0's line numbers.  `engine` is the prefix the lines carry on
/// this stream and `opener` the line printed before the traceback.
fn triton_engine_core(engine: &str, opener: &str) -> String {
    let mut stream = String::new();
    for line in [
        opener,
        "Traceback (most recent call last):",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/core.py\", line 1318, in run_engine_core",
        "    engine_core = EngineCoreProc(*args, engine_index=dp_rank, **kwargs)",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/core.py\", line 1074, in __init__",
        "    super().__init__(",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/core.py\", line 143, in __init__",
        "    kv_cache_config = self._initialize_kv_caches(vllm_config)",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/core.py\", line 331, in _initialize_kv_caches",
        "    self.model_executor.initialize_from_config(kv_cache_configs)",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/executor/abstract.py\", line 124, in initialize_from_config",
        "    compilation_times: list[CompilationTimes] = self.collective_rpc(",
        "                                                ^^^^^^^^^^^^^^^^^^^^",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/executor/multiproc_executor.py\", line 416, in collective_rpc",
        "    return future if non_block else future.result()",
        "                                    ^^^^^^^^^^^^^^^",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/executor/multiproc_executor.py\", line 91, in result",
        "    return super().result()",
        "           ^^^^^^^^^^^^^^^^",
        "  File \"/usr/lib/python3.12/concurrent/futures/_base.py\", line 449, in result",
        "    return self.__get_result()",
        "           ^^^^^^^^^^^^^^^^^^^",
        "  File \"/usr/lib/python3.12/concurrent/futures/_base.py\", line 401, in __get_result",
        "    raise self._exception",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/executor/multiproc_executor.py\", line 95, in _wait_for_response",
        "    response = self.aggregate(self.get_response())",
        "                              ^^^^^^^^^^^^^^^^^^^",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/executor/multiproc_executor.py\", line 405, in get_response",
        "    raise RuntimeError(",
        "RuntimeError: Worker failed with error 'Triton Error [CUDA]: an illegal memory access was encountered', please check the stack trace above for the root cause",
    ] {
        writeln!(stream, "{engine}{line}").unwrap();
    }
    stream
}

/// The worker's own report in vLLM v0.27.0's shape.  `worker_busy_loop`
/// adds `traceback.format_exc()` to the exception as a note and then calls
/// `logger.exception("WorkerProc hit an exception.")`
/// (`multiproc_executor.py:1018`), so stdout carries the traceback twice,
/// every line under the log record's prefix, and a bare prefix last.  The
/// production capture never retained this block, and the failing worker
/// may have been rank 1's, so this is a reconstruction: the vLLM and torch
/// frames are v0.27.0's and torch 2.14.0's, and the model and Triton frames
/// below `_dummy_run` are left out.
fn triton_worker() -> String {
    let worker = "(Worker_TP0 pid=512) ERROR 10-25 09:45:46 [multiproc_executor.py:1018] ";
    let traceback = [
        "Traceback (most recent call last):",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/executor/multiproc_executor.py\", line 1010, in worker_busy_loop",
        "    output = func(*args, **kwargs)",
        "             ^^^^^^^^^^^^^^^^^^^^^",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/worker/gpu_worker.py\", line 708, in compile_or_warm_up_model",
        "    self.model_runner._dummy_run(size, skip_eplb=True, remove_lora=False)",
        "  File \"/usr/local/lib/python3.12/dist-packages/torch/utils/_contextlib.py\", line 124, in decorate_context",
        "    return func(*args, **kwargs)",
        "           ^^^^^^^^^^^^^^^^^^^^^",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/worker/gpu_model_runner.py\", line 6156, in _dummy_run",
        "    outputs = self.model(",
        "              ^^^^^^^^^^^",
        "RuntimeError: Triton Error [CUDA]: an illegal memory access was encountered",
    ];
    let mut stream = String::new();
    for index in 0..20 {
        writeln!(stream, "(EngineCore pid=403) INFO 10-25 09:45:{index:02} [gpu_worker.py:707] Compile and warming up model for size {}", 8192 >> (index % 8)).unwrap();
    }
    writeln!(stream, "{worker}WorkerProc hit an exception.").unwrap();
    for line in traceback.iter().chain(&traceback) {
        writeln!(stream, "{worker}{line}").unwrap();
    }
    writeln!(stream, "{worker}").unwrap();
    stream
}

const TRITON_NCCL_AFTER: &str = "NCCL version 2.30.7+cuda13.0\n\n[2026-10-25 09:45:47] spark-3542:512:754 [0] misc/strongstream.cc:424 (ncclStrongStreamSynchronize) NCCL WARN Cuda failure 'an illegal memory access was encountered'\n\n[2026-10-25 09:45:47] spark-3542:512:754 [0] init.cc:2742 (commDestroySync) NCCL WARN commDestroySync: comm 0x3f51d530 rank 0 sync hostStream error 1\n\n[2026-10-25 09:45:47] spark-3542:512:754 [0] allocator.cc:132 (ncclMemFree) NCCL WARN Cuda failure 'an illegal memory access was encountered'\n\n[2026-10-25 09:45:48] spark-3542:512:594 [0] include/alloc.h:166 (ncclCudaHostFree) NCCL WARN Cuda failure 'an illegal memory access was encountered'\n(EngineCore pid=403) ERROR 10-25 09:45:51 [multiproc_executor.py:295] Worker proc VllmWorker-0 died unexpectedly (exit code: None), shutting down executor.\n";

#[test]
fn a_summary_that_asks_to_check_the_trace_above_yields_to_the_worker() {
    // Wrong implementation this catches: the pointer was a list of exact
    // phrases, so vLLM's "please check the stack trace above for the root
    // cause" read as a cause.  Production evidence kept 2048 bytes of the
    // engine core's own frames on both streams and its exit summary named
    // the pointer, not the worker's Triton error that it pointed at.
    let worker_cause = "(Worker_TP0 pid=512) ERROR 10-25 09:45:46 [multiproc_executor.py:1018] RuntimeError: Triton Error [CUDA]: an illegal memory access was encountered\n";
    // Records after the engine's report keep the worker's report out of
    // the plain tail.
    let after = "(EngineCore pid=403) INFO 10-25 09:45:52 [core.py:1] shutting down\n"
        .repeat(RETAINED_BYTES / 32);
    let stdout = triton_worker()
        + &triton_engine_core(
            "(EngineCore pid=403) ERROR 10-25 09:45:46 [core.py:1355] ",
            "EngineCore failed to start.",
        )
        + TRITON_NCCL_AFTER
        + &after;
    let stdout = stdout.as_bytes();
    let plain = String::from_utf8_lossy(&stdout[stdout.len() - RETAINED_BYTES..]);
    assert!(!plain.contains(worker_cause));
    let tail = retain(stdout);
    assert!(tail.text.contains(worker_cause), "{}", tail.text);
    assert!(tail.text.len() <= RETAINED_BYTES);
    // stderr holds only the engine core's uncaught pointer; with no cause
    // above it, it keeps ending on that pointer.
    let stderr = "(EngineCore pid=403) /usr/local/lib/python3.12/dist-packages/torch/cuda/__init__.py:63: FutureWarning: The pynvml package is deprecated.\n".repeat(4)
            + &triton_engine_core("(EngineCore pid=403) ", "Process EngineCore_DP0:");
    let capture = CapturedOutput::new(stdout, stderr.as_bytes());
    let logs = capture.retained();
    assert!(
        logs.stderr
            .text
            .ends_with("please check the stack trace above for the root cause\n")
    );
    // The pointer quotes the worker's message, so the exit summary names
    // the worker's exception, and stops where the next log record begins.
    let summary = exit_summary(Ok(&exit(1, false, "")), Some(&capture), None);
    assert!(
            summary.ends_with(" exception=\"RuntimeError: Triton Error [CUDA]: an illegal memory access was encountered\""),
            "{summary}"
        );
}

#[test]
fn a_pointer_keeps_the_exit_summary_when_the_other_stream_is_unrelated() {
    // Wrong implementation this catches: preferring any exception on the
    // other stream over a pointer named vLLM's caught, non-fatal registry
    // KeyError as the reason a start failed.
    let mut stdout = String::from(
        "(APIServer pid=55) ERROR 10-25 09:40:02 [registry.py:1033] Error in loading model architecture 'InklingForCausalLM'\n",
    );
    for line in [
        "Traceback (most recent call last):",
        "  File \"/usr/local/lib/python3.12/dist-packages/vllm/model_executor/models/registry.py\", line 1031, in _try_load_model_cls",
        "    return model.load_model_cls()",
        "KeyError: 'model_type'",
    ] {
        writeln!(
            stdout,
            "(APIServer pid=55) ERROR 10-25 09:40:02 [registry.py:1033] {line}"
        )
        .unwrap();
    }
    let stderr = "(APIServer pid=55) Traceback (most recent call last):\n(APIServer pid=55)   File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/utils.py\", line 1250, in wait_for_engine_startup\n(APIServer pid=55)     raise RuntimeError(\n(APIServer pid=55) RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}\n";
    let capture = CapturedOutput::new(stdout.as_bytes(), stderr.as_bytes());
    let summary = exit_summary(Ok(&exit(1, false, "")), Some(&capture), None);
    assert!(
            summary.ends_with(" exception=\"RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}\""),
            "{summary}"
        );
}

#[test]
fn a_one_token_exception_is_not_read_into_a_pointer() {
    // Wrong implementation this catches: matching the other stream's
    // message anywhere in the pointer let `RuntimeError: 1` take over the
    // summary because vLLM's pointer lists `{'EngineCore_DP0': 1}`.
    let stdout = "(APIServer pid=55) Traceback (most recent call last):\n(APIServer pid=55)   File \"/app/probe.py\", line 9, in probe\n(APIServer pid=55) RuntimeError: 1\n";
    let stderr = "(APIServer pid=55) Traceback (most recent call last):\n(APIServer pid=55)   File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/utils.py\", line 1250, in wait_for_engine_startup\n(APIServer pid=55)     raise RuntimeError(\n(APIServer pid=55) RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {'EngineCore_DP0': 1}\n";
    let capture = CapturedOutput::new(stdout.as_bytes(), stderr.as_bytes());
    let summary = exit_summary(Ok(&exit(1, false, "")), Some(&capture), None);
    assert!(
        summary.ends_with("Failed core proc(s): {'EngineCore_DP0': 1}\""),
        "{summary}"
    );
}

#[test]
fn a_parenthesised_message_line_is_not_a_process_prefix() {
    // Wrong implementation this catches: any `(...) ` opener was stripped
    // as a process prefix, so a message continuing on `(4, 8) vs (8, 4)`
    // looked like another process's line and was cut off.
    let stderr = "Traceback (most recent call last):\n  File \"/app/load.py\", line 3, in load\nValueError: weight shape mismatch for lm_head:\n(4, 8) vs (8, 4)\n";
    let exception = python_exception(&logs("", stderr)).expect("an exception");
    assert_eq!(
        exception,
        "ValueError: weight shape mismatch for lm_head: (4, 8) vs (8, 4)"
    );
}

#[test]
fn a_pointer_is_decided_by_shape_not_by_phrase() {
    // Wrong implementations this catches: a phrase list misses another
    // wording of the same pointer, a rule that ignores word order reads a
    // cause as a pointer, and `previous` as a place reads vLLM's OOM hint
    // ("check ... the previous run") as one.  Every line is a real upstream message
    // (vLLM v0.27.0, sglang main, torch 2.14.0, CPython).
    for pointer in [
        "Exception: WorkerProc initialization failed due to an exception in a background process. See stack trace for root cause.",
        "RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}",
        "RuntimeError: Worker failed with error 'x', please check the stack trace above for the root cause",
        "EngineDeadError: EngineCore encountered an issue. See stack trace (above) for the root cause.",
        "RuntimeError: Initialization failed. Please see the error messages above.",
        "the kernels failed to build, see the warning above",
        "RuntimeError: We ran into an error when deserializing the saved file. Please check the warnings above for possible errors.",
        "(Refer to the full stack trace above for more information.)",
    ] {
        assert!(points_elsewhere(pointer.as_bytes()), "{pointer}");
    }
    for cause in [
        "RuntimeError: kv-canary: kernel_run_counter did not increase since previous check for tags=[DECODE] at step=128; canary path is not executing",
        "Trace file integrity check failed: Expecting value: line 1 column 1 (char 0)",
        "RuntimeError: vLLM failed to compile the model. The most likely reason for this is that a previous compilation failed, leading to a corrupted compilation artifact. We recommend trying to remove ~/.cache/vllm/torch_compile_cache and try again to see the real issue.",
        "Use `torch._assert()` to raise a hard AssertionError when the check fails. This error will propagate back the user code that called the compiled function (i.e. Dynamo will not trace any exception handling).",
        "To enable traceback see: https://pytorch.org/docs/stable/elastic/errors.html",
        "Initial free memory 101.2 GiB, reserved 80.0 GiB memory for KV Cache as specified by kv_cache_memory_bytes config and skipped memory profiling. This does not respect the gpu_memory_utilization config. Only use kv_cache_memory_bytes config when you want manual control of KV cache memory size. If OOM'ed, check the difference of initial free memory between the current run and the previous run where kv_cache_memory_bytes is suggested and update it correspondingly.",
        "Previous Traceback:",
        "ValueError: Should be unreachable - `return params` above must be reached: params=None",
        "The above exception was the direct cause of the following exception:",
        "During handling of the above exception, another exception occurred:",
        "CUDA kernel errors might be asynchronously reported at some other API call, so the stacktrace below might be incorrect.",
    ] {
        assert!(!points_elsewhere(cause.as_bytes()), "{cause}");
    }
}

#[test]
fn a_cause_that_says_previous_check_is_not_a_pointer() {
    // Wrong implementation this catches: an order-blind rule read sglang's
    // kv-canary failure ("check" and "previous") as a pointer, so with an
    // earlier traceback in the capture the window gave way to it and kept
    // neither error.
    let canary = "RuntimeError: kv-canary: kernel_run_counter did not increase since previous check for tags=[DECODE] at step=128; canary path is not executing\n";
    let mut stream = b"Traceback (most recent call last):\n  File \"warmup.py\", line 3, in probe\nKeyError: 'optional_feature'\n".to_vec();
    stream.extend(b"Traceback (most recent call last):\n");
    for _ in 0..30 * SCALE {
        stream.extend(b"  File \"/sgl-workspace/sglang/python/sglang/srt/kv_canary/runner/health_checker.py\", line 73, in _postprocess_on_host\n    raise RuntimeError(\n");
    }
    stream.extend(canary.as_bytes());
    let tail = retain(&stream);
    assert!(tail.text.ends_with(canary), "{}", tail.text);
}

#[test]
fn a_block_that_names_its_own_cause_is_kept() {
    // The same rule must not drop a summary that is itself the answer.
    let mut stream =
        b"(EngineCore pid=177) ERROR [core.py:1355] Traceback (most recent call last):\n".to_vec();
    stream.extend_from_slice(
        b"(EngineCore pid=177) ERROR [core.py:1355]   File \"x.py\", line 1, in y\n",
    );
    stream.extend_from_slice(b"(EngineCore pid=177) ERROR [core.py:1355] ValueError: drafter checkpoint is incompatible\n");
    let tail = retain(&stream);
    assert!(
        tail.text.contains("drafter checkpoint is incompatible"),
        "{}",
        tail.text
    );
}

#[test]
fn the_window_ends_at_the_block_that_reports_the_cause() {
    // Wrong implementation this catches: retaining the newest bytes keeps
    // the block that says "see root cause above" and its boilerplate
    // frames, and spends the whole budget restating that the cause is
    // elsewhere. The block's own frames are what the window drops.
    let mut stream = b"ERROR drafter checkpoint is incompatible\n".to_vec();
    stream.extend(cut_off_traceback());
    stream.extend(b"tail line\n".repeat(40));
    let tail = retain(&stream);
    assert!(tail.text.contains("drafter checkpoint is incompatible"));
    assert!(tail.text.contains("Traceback (most recent call last)"));
    // The frames below the reporting block are not retained.
    assert!(!tail.text.contains("tail line"), "{}", tail.text);
    assert!(tail.text.len() <= RETAINED_BYTES);
}

#[test]
fn every_retained_window_says_how_much_it_dropped() {
    let stream = b"noise\n".repeat(4000);
    let tail = retain(&stream);
    assert!(tail.truncated);
    assert_eq!(
        tail.dropped_bytes,
        Some((stream.len() - tail.text.len()) as u64)
    );
    assert!(tail.text.len() <= RETAINED_BYTES);
}

#[test]
fn the_streams_are_retained_independently() {
    let mut stderr = cut_off_traceback();
    stderr.extend_from_slice(b"Exception: WorkerProc initialization failed\n");
    let logs = retain_container(b"stdout before the failure\n", &stderr);
    assert_eq!(logs.stdout.text, "stdout before the failure\n");
    assert!(logs.stderr.text.contains("from_vllm_config"));
    assert!(
        logs.stderr
            .text
            .contains("Exception: WorkerProc initialization failed")
    );
}

#[test]
fn an_empty_stream_is_reported_as_empty() {
    let tail = retain(&[]);
    assert_eq!(tail.text, "");
    assert_eq!(tail.dropped_bytes, Some(0));
    assert!(!tail.truncated);
}

fn exit(code: i32, oom: bool, error: &str) -> ContainerExit {
    ContainerExit {
        exit_code: Some(code),
        oom_killed: Some(oom),
        error: error.to_owned(),
        started_at: None,
        finished_at: None,
        gpu_xid: None,
        host_memory: None,
    }
}

fn logs(stdout: &str, stderr: &str) -> HostHelperProcessLogs {
    retain_container(stdout.as_bytes(), stderr.as_bytes())
}

fn captured(stdout: &str, stderr: &str) -> CapturedOutput {
    CapturedOutput::new(stdout.as_bytes(), stderr.as_bytes())
}

#[test]
fn the_exit_state_is_read_back_from_the_inspect_format() {
    let state = parse_exit(b"137\ttrue\t0001-01-01T00:00:00Z\t0001-01-01T00:00:00Z\t\n").unwrap();
    assert_eq!(state, exit(137, true, ""));
    let state = parse_exit(
        b"1\tfalse\t2026-10-01T00:00:00Z\t2026-10-01T00:10:00.5Z\tfailed: exec\tformat error\n",
    )
    .unwrap();
    assert_eq!(state.error, "failed: execformat error");
    assert!(state.started_at.is_some() && state.finished_at.is_some());
    assert!(parse_exit(b"not-a-number\ttrue\t").is_none());
    assert!(parse_exit(b"").is_none());
}

#[test]
fn common_exit_causes_are_named() {
    // Wrong implementation this catches: every exit read as `unclassified`,
    // which is how a silent OOM-kill looked like any other crash.
    let silent = captured("", "");
    for (state, output, cause) in [
        (exit(137, true, ""), &silent, ExitCause::OomKilled),
        (exit(137, false, ""), &silent, ExitCause::CrashedBySignal),
        (
            exit(255, false, "exec format error"),
            &silent,
            ExitCause::ImageArchMismatch,
        ),
        (exit(127, false, ""), &silent, ExitCause::MissingFile),
        (
            exit(1, false, ""),
            &captured("", "error: unknown argument: --foo\n"),
            ExitCause::BadArguments,
        ),
        (
            exit(1, false, ""),
            &captured(
                "",
                "gguf_init_from_file: failed to open '/models/x.gguf': No such file or directory\n",
            ),
            ExitCause::MissingFile,
        ),
        (
            exit(1, false, ""),
            &captured(
                "",
                "CUDA error: no kernel image is available for execution on the device\n",
            ),
            ExitCause::UnsupportedGpuArch,
        ),
        (exit(139, false, ""), &silent, ExitCause::CrashedBySignal),
        (exit(3, false, ""), &silent, ExitCause::Unclassified),
    ] {
        assert_eq!(classify(Some(&state), Some(output)), cause, "{state:?}");
    }
    // A normal GPU banner is not a failure marker.
    assert_eq!(
        classify(
            Some(&exit(3, false, "")),
            Some(&captured(
                "Device 0: NVIDIA GB10, compute capability 12.1\n",
                ""
            ))
        ),
        ExitCause::Unclassified
    );
}

/// The live shape: sglang refusing a model's attention backend at start.
/// The frames alone exceed the budget, and the exception's message runs on
/// over the lines below it.
fn sglang_backend_refusal() -> Vec<u8> {
    let mut stream =
        b"[2026-10-15 08:12:00] server_args=ServerArgs(model_path='/models/gpt-oss')\n".to_vec();
    stream.extend(b"Traceback (most recent call last):\n");
    for (index, function) in [
        "<module>",
        "main",
        "prepare_server_args",
        "from_cli_args",
        "__init__",
        "__post_init__",
        "_handle_model_specific_adjustments",
        "_handle_attention_backend_compatibility",
    ]
    .iter()
    .cycle()
    .take(24 * SCALE)
    .enumerate()
    {
        stream.extend(
                format!(
                    "  File \"/sgl-workspace/sglang/python/sglang/srt/server_args.py\", line {}, in {function}\n    self.{function}()\n",
                    100 + index
                )
                .into_bytes(),
            );
    }
    stream.extend(b"AssertionError: GptOssForCausalLM requires one of ['triton', 'trtllm_mha', 'fa3', 'fa4', 'ascend', 'intel_amx', 'intel_xpu', 'aiter'] attention backend, but got the following backends\n");
    stream.extend(b"- Prefill: flashinfer\n- Decode: flashinfer\n");
    stream
}

#[test]
fn a_multi_line_exception_message_is_retained_to_its_last_line() {
    // Wrong implementation this catches: the window ended on the
    // `AssertionError` line, so the live capture stopped at "but got the
    // following backends" and dropped the backends it named.
    let stream = sglang_backend_refusal();
    assert!(stream.len() > RETAINED_BYTES);
    let tail = retain(&stream);
    assert!(
        tail.text.ends_with("- Decode: flashinfer\n"),
        "{}",
        tail.text
    );
    assert!(tail.text.contains("AssertionError: GptOssForCausalLM"));
    // The frame that raised it stays above it.
    assert!(
        tail.text
            .contains("in _handle_attention_backend_compatibility"),
        "{}",
        tail.text
    );
    assert!(tail.truncated);
    assert!(tail.text.len() <= RETAINED_BYTES);
}

#[test]
fn output_after_an_exception_is_kept_while_the_exception_stays_in_the_window() {
    // The stream goes on past the message for longer than the budget: the
    // window keeps the exception line, its message and as much of what
    // followed as fits, ending on a whole line.
    let mut stream = sglang_backend_refusal();
    stream.extend(b"[2026-10-15 08:12:01] Received sigquit from a child process. It usually means the child failed.\n".repeat(28));
    let tail = retain(&stream);
    assert!(
        tail.text.contains("AssertionError: GptOssForCausalLM"),
        "{}",
        tail.text
    );
    assert!(tail.text.contains("- Decode: flashinfer"), "{}", tail.text);
    assert!(tail.text.contains("Received sigquit"), "{}", tail.text);
    assert!(tail.text.ends_with('\n'));
    assert!(tail.text.len() <= RETAINED_BYTES);
}

#[test]
fn an_uncaught_python_exception_is_named_with_its_message() {
    let output = captured("", std::str::from_utf8(&sglang_backend_refusal()).unwrap());
    let state = exit(1, false, "");
    assert_eq!(
        classify(Some(&state), Some(&output)),
        ExitCause::PythonException
    );
    let summary = exit_summary(Ok(&state), Some(&output), None);
    assert!(
            summary.starts_with("exit_code=1 oom_killed=false exit_cause=python_exception exception=\"AssertionError: GptOssForCausalLM requires one of ["),
            "{summary}"
        );
    assert!(
        summary.contains(
            "but got the following backends - Prefill: flashinfer - Decode: flashinfer\""
        ),
        "{summary}"
    );
    // The helper sends at most 480 characters; the facts and the
    // exception both fit.
    assert!(summary.chars().count() <= 480, "{summary}");
}

/// Live shape (2026-10-28): sglang raises at startup, then SIGKILLs its own
/// process tree, so Docker reports 137 with OOMKilled=false.
const SGLANG_SELF_KILL: &str = "Traceback (most recent call last):\n  File \"/sgl/engine.py\", line 9, in init\nRuntimeError: Hybrid (mamba/linear-attention) state cache is too small\nReceived sigquit from a child process. It usually means the child failed.\nkill_process_tree called: parent_pid=55, include_parent=True\n";

#[test]
fn a_bare_137_is_never_an_oom_kill() {
    // Class guard: only the runtime's own OOM fact names `oom_killed`; a
    // 137 with OOMKilled=false is a plain SIGKILL, whatever the log says.
    let output = captured("", SGLANG_SELF_KILL);
    assert_eq!(
        classify(Some(&exit(137, false, "")), Some(&output)),
        ExitCause::PythonException
    );
    for tail in ["", SGLANG_SELF_KILL, "Killed\n", "out of memory\n"] {
        assert_ne!(
            classify(Some(&exit(137, false, "")), Some(&captured("", tail))),
            ExitCause::OomKilled,
            "{tail:?}"
        );
    }
    assert_eq!(
        classify(Some(&exit(137, true, "")), Some(&output)),
        ExitCause::OomKilled
    );
    assert!(ExitCause::CrashedBySignal.skipped_cleanup());
}

#[test]
fn an_exception_message_is_bounded() {
    let mut stream =
        b"Traceback (most recent call last):\n  File \"x.py\", line 1, in f\n".to_vec();
    stream.extend(format!("ValueError: {}\n", "x ".repeat(400)).into_bytes());
    let exception =
        python_exception(&logs("", std::str::from_utf8(&stream).unwrap())).expect("an exception");
    assert!(exception.starts_with("ValueError: x x"), "{exception}");
    assert_eq!(exception.chars().count(), EXCEPTION_CHARS);
    // An exception line longer than the whole budget still yields a
    // bounded window rather than a slice past the line's end.
    stream.extend(format!("ValueError: {}\n", "x ".repeat(2000 * SCALE)).into_bytes());
    let tail = retain(&stream);
    assert!(tail.text.len() <= RETAINED_BYTES);
    assert!(tail.truncated);
}

#[test]
fn a_named_cause_keeps_its_name_and_carries_the_exception() {
    let stderr = "Traceback (most recent call last):\n  File \"/app/load.py\", line 3, in load\n    open(path)\nFileNotFoundError: [Errno 2] No such file or directory: '/models/x.gguf'\n";
    let state = exit(1, false, "");
    let output = captured("", stderr);
    assert_eq!(
        classify(Some(&state), Some(&output)),
        ExitCause::MissingFile
    );
    let summary = exit_summary(Ok(&state), Some(&output), None);
    assert!(
        summary.contains("exit_cause=missing_file exception=\"FileNotFoundError: [Errno 2]"),
        "{summary}"
    );
}

#[test]
fn output_that_is_not_a_traceback_is_not_an_exception() {
    // A capitalised `Word:` line outside a traceback is ordinary output.
    let output = captured(
        "INFO: Started server process\nWARNING: low memory\n",
        "Error: model not ready\n",
    );
    let state = exit(3, false, "");
    assert_eq!(
        classify(Some(&state), Some(&output)),
        ExitCause::Unclassified
    );
    assert!(python_exception(output.retained()).is_none());
    assert!(!exit_summary(Ok(&state), Some(&output), None).contains("exception="));
}

#[test]
fn an_exit_summary_is_never_empty_and_names_what_could_not_be_read() {
    // The class guard: whatever the helper could or could not read, a
    // process-exit failure says what happened or why that is unknown.
    let states: [Result<&ContainerExit, &'static str>; 2] = [
        Ok(&ContainerExit {
            exit_code: Some(1),
            oom_killed: Some(false),
            error: String::new(),
            started_at: None,
            finished_at: None,
            gpu_xid: None,
            host_memory: None,
        }),
        Err("inspect failed"),
    ];
    let empty = captured("", "");
    let some = captured("", "boom\n");
    for state in states {
        for (output, error) in [
            (None, Some("log command failed")),
            (None, None),
            (Some(&empty), None),
            (Some(&some), None),
        ] {
            let summary = exit_summary(state, output, error);
            assert!(summary.contains("exit_cause="), "{summary}");
            assert!(
                summary.contains("exit_code=") || summary.contains("exit_state_unavailable="),
                "{summary}"
            );
            if output.is_none() {
                assert!(summary.contains("logs_unavailable="), "{summary}");
            }
            if output == Some(&empty) {
                assert!(summary.contains("no_output=true"), "{summary}");
            }
        }
    }
}
