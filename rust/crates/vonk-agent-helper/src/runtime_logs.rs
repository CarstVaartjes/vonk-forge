//! The failed container's own output, retained per stream.
//!
//! The helper is the only party that ever holds a container's log, so it owns
//! the retention bound and reports what it dropped.  Two properties matter more
//! than the bound itself:
//!
//! * The streams stay apart.  Merging them into one document can only ever show
//!   one tail, and whichever stream is written first is the one silently lost.
//! * The window keeps the cause.  A workload that dies while printing a Python
//!   traceback leaves the block cut off wherever its stdio buffer stopped, so
//!   the final bytes of the stream are frames and the exception those frames
//!   were explaining sits above them.  A plain tail therefore shows the frames
//!   and hides the reason, so the window is anchored before a failure header
//!   when the capture contains one.

use vonk_agent_protocol::generated::{FailureLogTail, HostHelperProcessLogs};

/// Bytes retained per container stream.
///
/// The protocol declares 2048 for this field, but the consumer that actually
/// displays it keeps only the first 1024 characters of whatever arrives.  The
/// single owner of the retention bound retains what is shown, so the window it
/// chooses is the one the operator reads.
const RETAINED_BYTES: usize = 1024;
/// Lines read back from the container.  Kept well above what is retained so a
/// failure header above the retained window is still visible to the anchor.
pub const CAPTURE_LINES: &str = "400";

/// Headers that open a block reporting why a process stopped.
///
/// A header is matched anywhere in its own line because a supervised process
/// prefixes its children's output: the live observation that motivated this
/// module reads `(EngineCore pid=177) ERROR ... File "...multiproc_executor.py",
/// line 210, in _init_executor`, where nothing begins with `ERROR`.
const FAILURE_HEADERS: [&str; 12] = [
    "Traceback (most recent call last)",
    "RuntimeError:",
    "ValueError:",
    "ImportError:",
    "ModuleNotFoundError:",
    "KeyError:",
    "AssertionError:",
    "Exception:",
    "FATAL",
    "Fatal error",
    // The separator is part of the header so a line such as `ERROR_CODE=...`
    // cannot move the window.
    "ERROR ",
    "ERROR:",
];

/// A block that names itself as a summary of a cause printed above it.
///
/// vLLM's executor ends a worker failure with "See stack trace for root cause."
/// and its engine ends with "See root cause above.".  A window that keeps such a
/// block keeps a pointer instead of the cause, so it ends at the start of the
/// block and carries what the block reports.
const SELF_REFERENTIAL: [&str; 3] = [
    "see stack trace for root cause",
    "see root cause above",
    "see above",
];

const TRACEBACK: &[u8] = b"Traceback (most recent call last)";

fn contains(line: &[u8], needle: &[u8]) -> bool {
    line.len() >= needle.len() && line.windows(needle.len()).any(|window| window == needle)
}

fn contains_ascii_case(text: &[u8], needle: &str) -> bool {
    let needle = needle.as_bytes();
    text.len() >= needle.len()
        && text
            .windows(needle.len())
            .any(|window| window.eq_ignore_ascii_case(needle))
}

/// The last line that reports a failure: its start, its end, the start of the
/// traceback block it belongs to, and the start of the block above that one.
///
/// The scan is over the raw bytes: decoding first would make every line longer
/// than the bytes it came from whenever the container wrote a byte that is not
/// valid UTF-8, and an offset measured in decoded characters would then name a
/// position the stream does not have -- both a wrong window and a panic in the
/// slice below.
fn last_failure_line(stream: &[u8]) -> Option<(usize, usize, usize, Option<usize>)> {
    let mut last_traceback = None;
    let mut previous_traceback = None;
    let mut header = None;
    let mut offset = 0;
    for line in stream.split_inclusive(|byte| *byte == b'\n') {
        let start = offset;
        let end = offset + line.len();
        offset = end;
        if contains(line, TRACEBACK) {
            previous_traceback = last_traceback;
            last_traceback = Some(start);
        }
        if FAILURE_HEADERS
            .iter()
            .any(|marker| contains(line, marker.as_bytes()))
        {
            let block = last_traceback.unwrap_or(start);
            let cause = previous_traceback.filter(|earlier| *earlier < block);
            header = Some((start, end, block, cause));
        }
    }
    header
}

/// The text of a log line without the prefix a supervised process adds
/// (`(APIServer pid=54) `) or the engine's `ERROR 10-06 05:05:21 [file:1] `.
fn line_content(line: &[u8]) -> String {
    let text = String::from_utf8_lossy(line);
    let mut rest: &str = text.trim_end_matches(['\r', '\n']);
    if rest.starts_with('(')
        && let Some(index) = rest.find(") ")
    {
        rest = &rest[index + 2..];
    }
    let level = rest.trim_start();
    if ["ERROR ", "WARNING ", "INFO "]
        .iter()
        .any(|prefix| level.starts_with(prefix))
        && let Some(index) = level.find("] ")
    {
        return level[index + 2..].to_owned();
    }
    rest.to_owned()
}

/// Whether a line continues a traceback block (frame, source line, caret
/// marker, or the chaining sentence between two blocks) rather than closing it.
fn continues_traceback(line: &[u8]) -> bool {
    let content = line_content(line);
    let trimmed = content.trim_start();
    trimmed.is_empty()
        || content.starts_with(char::is_whitespace)
        || trimmed.starts_with("File \"")
        || trimmed
            .chars()
            .all(|character| matches!(character, '^' | '~' | ' '))
        || trimmed.starts_with("During handling of the above exception")
        || trimmed.starts_with("The above exception was the direct cause")
}

/// The end of the exception line that closes the traceback opened by the line
/// ending at `opener_end`, or `None` for a block cut off before its exception.
fn traceback_exception_end(stream: &[u8], opener_end: usize) -> Option<usize> {
    let mut offset = opener_end;
    for line in stream[opener_end..].split_inclusive(|byte| *byte == b'\n') {
        offset += line.len();
        if contains(line, TRACEBACK) {
            return None;
        }
        if !continues_traceback(line) {
            // Whatever follows a block that never printed its exception is
            // ordinary output, not the cause.
            return names_an_exception(line).then_some(offset);
        }
    }
    None
}

/// Whether a line opens with an exception class (`OSError: ...`,
/// `torch.OutOfMemoryError: ...`) or is one bare (`KeyboardInterrupt`).
fn names_an_exception(line: &[u8]) -> bool {
    let content = line_content(line);
    let name = content.split(':').next().unwrap_or_default().trim();
    let class = name.rsplit('.').next().unwrap_or_default();
    !name.is_empty()
        && name
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, '_' | '.'))
        && class.starts_with(|character: char| character.is_ascii_uppercase())
}

/// The retained byte range of one stream: an offset and a length.
///
/// The window ends where the stream's own account of the failure ends, so it
/// carries the cause rather than a pointer to it.  A workload whose background
/// worker dies prints the worker's traceback first and a summary second:
///
/// ```text
/// (WorkerProc pid=200) Traceback (most recent call last):
/// (WorkerProc pid=200)   File "...", line 300, in init_device
/// (WorkerProc pid=200) RuntimeError: <why the worker died>
/// (EngineCore pid=177) ERROR ... Traceback (most recent call last):
/// (EngineCore pid=177) ERROR ... Exception: WorkerProc initialization failed
///     due to an exception in a background process. See stack trace for root cause.
/// ```
///
/// The summary occupies the whole budget restating that the cause is above it,
/// so a block that points above itself ends the window at its own start.  A
/// block that names its own cause ends the window at itself, and a stream with
/// no reporting block keeps its newest bytes.
fn window(stream: &[u8]) -> (usize, usize) {
    let Some((header_start, header_end, block_start, cause_above)) = last_failure_line(stream)
    else {
        let start = stream.len().saturating_sub(RETAINED_BYTES);
        return (start, stream.len() - start);
    };
    // Give way only when the cause above is itself a failure block.  A summary
    // that names its own cause -- "Engine core initialization failed" -- is more
    // than a pointer, and there is nothing above it to prefer.
    // A traceback ends with the exception that explains it.  That exception's
    // type is any class name, so the header list cannot name it; when the last
    // failure line is the opener itself, the block's own closing line is the
    // cause and the window ends there instead of on the opener.
    let header_end = if header_start == block_start {
        traceback_exception_end(stream, header_end).unwrap_or(header_end)
    } else {
        header_end
    };
    let end = if header_start > block_start
        && cause_above.is_some()
        && SELF_REFERENTIAL
            .iter()
            .any(|marker| contains_ascii_case(&stream[block_start..header_end], marker))
    {
        block_start
    } else {
        header_end
    };
    let start = end.saturating_sub(RETAINED_BYTES);
    (start, end - start)
}

/// Retain one stream's window without breaking a line in half, and report what
/// was dropped so a window can never be mistaken for the whole log.
pub fn retain(stream: &[u8]) -> FailureLogTail {
    let (start, length) = window(stream);
    let selected = &stream[start..(start + length).min(stream.len())];
    // A window that opens mid-line must not present a partial line as if the
    // process had written it; one that opens on a line boundary keeps it.
    let selected = if start > 0 && stream[start - 1] != b'\n' {
        match selected.iter().position(|byte| *byte == b'\n') {
            Some(index) => &selected[index + 1..],
            None => &[][..],
        }
    } else {
        selected
    };
    let text = String::from_utf8_lossy(selected);
    let retained_lines = text.lines().count();
    let total_lines = stream.iter().filter(|byte| **byte == b'\n').count();
    let dropped = (stream.len() - selected.len()) as u64;
    FailureLogTail {
        text: text.into_owned(),
        truncated: dropped > 0,
        dropped_bytes: Some(dropped),
        dropped_lines: Some(total_lines.saturating_sub(retained_lines) as u64),
    }
}

/// The retained output of one failed container, per stream.
pub fn retain_container(stdout: &[u8], stderr: &[u8]) -> HostHelperProcessLogs {
    HostHelperProcessLogs {
        stdout: retain(stdout),
        stderr: retain(stderr),
    }
}

/// How the container runtime says the container ended.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ContainerExit {
    pub exit_code: Option<i32>,
    pub oom_killed: Option<bool>,
    /// The runtime's own start/exit error (`exec format error`, a missing
    /// executable, a refused device), empty when it recorded none.
    pub error: String,
}

/// Format for `docker container inspect --format`, read back by `parse_exit`.
pub const EXIT_FORMAT: &str = "{{.State.ExitCode}}\t{{.State.OOMKilled}}\t{{.State.Error}}";

/// Read the three fields of `EXIT_FORMAT`; the error text may hold tabs.
pub fn parse_exit(output: &[u8]) -> Option<ContainerExit> {
    let text = std::str::from_utf8(output).ok()?.trim_end_matches('\n');
    let mut fields = text.splitn(3, '\t');
    let exit_code = fields.next()?.trim().parse::<i32>().ok()?;
    let oom_killed = match fields.next()?.trim() {
        "true" => Some(true),
        "false" => Some(false),
        _ => None,
    };
    let error: String = fields
        .next()
        .unwrap_or("")
        .chars()
        .filter(|c| !c.is_control())
        .take(160)
        .collect();
    Some(ContainerExit {
        exit_code: Some(exit_code),
        oom_killed,
        error,
    })
}

/// Why a container exited, as a stable token the Controller can show and group.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExitCause {
    OomKilled,
    BadArguments,
    MissingFile,
    UnsupportedGpuArch,
    ImageArchMismatch,
    StartRefused,
    CrashedBySignal,
    Unclassified,
}

impl ExitCause {
    pub fn token(self) -> &'static str {
        match self {
            Self::OomKilled => "oom_killed",
            Self::BadArguments => "bad_arguments",
            Self::MissingFile => "missing_file",
            Self::UnsupportedGpuArch => "unsupported_gpu_arch",
            Self::ImageArchMismatch => "image_arch_mismatch",
            Self::StartRefused => "start_refused",
            Self::CrashedBySignal => "crashed_by_signal",
            Self::Unclassified => "unclassified",
        }
    }
}

fn has(text: &str, needles: &[&str]) -> bool {
    needles.iter().any(|needle| text.contains(needle))
}

/// Classify from the exit state first (it is authoritative), then from the
/// container's own words.  Only the retained tails are read.
pub fn classify(exit: Option<&ContainerExit>, logs: Option<&HostHelperProcessLogs>) -> ExitCause {
    let output = logs
        .map(|logs| format!("{}\n{}", logs.stdout.text, logs.stderr.text).to_lowercase())
        .unwrap_or_default();
    let runtime_error = exit.map(|e| e.error.to_lowercase()).unwrap_or_default();
    let code = exit.and_then(|e| e.exit_code);
    if exit.and_then(|e| e.oom_killed) == Some(true) || code == Some(137) {
        return ExitCause::OomKilled;
    }
    if has(&runtime_error, &["exec format error"]) {
        return ExitCause::ImageArchMismatch;
    }
    if has(
        &runtime_error,
        &[
            "executable file not found",
            "no such file",
            "not found in $path",
        ],
    ) || code == Some(127)
    {
        return ExitCause::MissingFile;
    }
    if !runtime_error.is_empty() {
        return ExitCause::StartRefused;
    }
    if has(
        &output,
        &[
            "no kernel image is available",
            "nokernelimagefordevice",
            "unsupported gpu architecture",
            "invalid device function",
        ],
    ) {
        return ExitCause::UnsupportedGpuArch;
    }
    if has(
        &output,
        &[
            "unknown argument",
            "unrecognized argument",
            "unrecognized option",
            "invalid argument",
            "error: invalid",
            "usage:",
        ],
    ) {
        return ExitCause::BadArguments;
    }
    if has(
        &output,
        &[
            "no such file or directory",
            "failed to open",
            "file not found",
            "failed to load model",
            "error loading model",
        ],
    ) {
        return ExitCause::MissingFile;
    }
    if matches!(code, Some(134 | 135 | 139)) {
        return ExitCause::CrashedBySignal;
    }
    ExitCause::Unclassified
}

/// The one-line, machine-readable account of an exited container.  It is never
/// empty: when the runtime could not be asked, that is itself the account, so a
/// process-exit failure always says either what happened or why that is unknown.
pub fn exit_summary(
    exit: Result<&ContainerExit, &'static str>,
    logs: Option<&HostHelperProcessLogs>,
    log_error: Option<&'static str>,
) -> String {
    let cause = classify(exit.ok(), logs);
    let mut parts = Vec::new();
    match exit {
        Ok(state) => {
            if let Some(code) = state.exit_code {
                parts.push(format!("exit_code={code}"));
            }
            if let Some(oom) = state.oom_killed {
                parts.push(format!("oom_killed={oom}"));
            }
            if !state.error.is_empty() {
                parts.push(format!("runtime_error={:?}", state.error));
            }
        }
        Err(reason) => parts.push(format!("exit_state_unavailable={reason:?}")),
    }
    parts.push(format!("exit_cause={}", cause.token()));
    match (logs, log_error) {
        (Some(logs), _) if logs.stdout.text.is_empty() && logs.stderr.text.is_empty() => {
            parts.push("no_output=true".to_owned())
        }
        (None, Some(reason)) => parts.push(format!("logs_unavailable={reason:?}")),
        (None, None) => parts.push("logs_unavailable=\"not captured\"".to_owned()),
        _ => {}
    }
    parts.join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A traceback with no exception line, as a process killed mid-write leaves
    /// it: the frames are the last thing written.
    fn cut_off_traceback() -> Vec<u8> {
        let mut value = String::from("Traceback (most recent call last):\n");
        for index in 0..24 {
            value.push_str(&format!(
                "(APIServer pid=56) File \"/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/async_llm.py\", line {}, in from_vllm_config\n",
                200 + index
            ));
            value.push_str("(APIServer pid=56)     return cls(\n");
            value.push_str("(APIServer pid=56) ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n");
        }
        value.into_bytes()
    }

    fn preceded_by_cause() -> Vec<u8> {
        let mut stream =
            b"NCCL WARN Call to ibv_reg_mr failed\nERROR drafter checkpoint is incompatible\n"
                .to_vec();
        stream.extend(cut_off_traceback());
        stream
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
        assert!(!tail.text.is_empty());
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
        let mut stream = b"zz\n".repeat(1000);
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

    #[test]
    fn a_block_that_names_its_own_cause_is_kept() {
        // The same rule must not drop a summary that is itself the answer.
        let mut stream =
            b"(EngineCore pid=177) ERROR [core.py:1355] Traceback (most recent call last):\n"
                .to_vec();
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
        }
    }

    fn logs(stdout: &str, stderr: &str) -> HostHelperProcessLogs {
        retain_container(stdout.as_bytes(), stderr.as_bytes())
    }

    #[test]
    fn the_exit_state_is_read_back_from_the_inspect_format() {
        let state = parse_exit(b"137\ttrue\t\n").unwrap();
        assert_eq!(state, exit(137, true, ""));
        let state = parse_exit(b"1\tfalse\tfailed: exec format error\n").unwrap();
        assert_eq!(state.error, "failed: exec format error");
        assert!(parse_exit(b"not-a-number\ttrue\t").is_none());
        assert!(parse_exit(b"").is_none());
    }

    #[test]
    fn common_exit_causes_are_named() {
        // Wrong implementation this catches: every exit read as `unclassified`,
        // which is how a silent OOM-kill looked like any other crash.
        let silent = logs("", "");
        for (state, output, cause) in [
            (exit(137, true, ""), &silent, ExitCause::OomKilled),
            (exit(137, false, ""), &silent, ExitCause::OomKilled),
            (
                exit(255, false, "exec format error"),
                &silent,
                ExitCause::ImageArchMismatch,
            ),
            (exit(127, false, ""), &silent, ExitCause::MissingFile),
            (
                exit(1, false, ""),
                &logs("", "error: unknown argument: --foo\n"),
                ExitCause::BadArguments,
            ),
            (
                exit(1, false, ""),
                &logs(
                    "",
                    "gguf_init_from_file: failed to open '/models/x.gguf': No such file or directory\n",
                ),
                ExitCause::MissingFile,
            ),
            (
                exit(1, false, ""),
                &logs(
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
                Some(&logs(
                    "Device 0: NVIDIA GB10, compute capability 12.1\n",
                    ""
                ))
            ),
            ExitCause::Unclassified
        );
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
            }),
            Err("inspect failed"),
        ];
        let empty = logs("", "");
        let some = logs("", "boom\n");
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
}
