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
//! * The window keeps what the exception said.  A traceback that did complete
//!   ends with its exception, and that exception's message may run over several
//!   lines (sglang names the attention backends it was given on the lines below
//!   its `AssertionError`).  A window that ends on the exception's first line
//!   drops the rest of the message, so it runs on to the stream's end as far as
//!   the budget allows, keeping the frame that raised it above.
//! * The window keeps a cause that is not a traceback.  vLLM's API server ends
//!   an engine failure with a long traceback closing "See root cause above",
//!   while the engine process printed its own error lines first.  When nothing
//!   above that summary is a traceback, its first error lines are retained
//!   ahead of the tail, with the omitted middle marked.

use std::ops::Range;
use vonk_agent_protocol::failure_evidence::omission_marker;
use vonk_agent_protocol::generated::{FailureLogTail, HostHelperProcessLogs};

/// Bytes retained per container stream: the protocol's bound for this field.
///
/// Every later reader keeps it whole: the agent and the Controller each bound
/// the text to 2048 from its end, and the evidence bundle and the operator log
/// projection carry it as it arrives.
const RETAINED_BYTES: usize = 2048;
/// Bytes reserved for the line that marks the output omitted between the
/// retained failure lines and the tail.
const OMISSION_MARKER_BYTES: usize = 48;
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

/// What a line reporting a failure contains, matched case-sensitively so that
/// ordinary words (`error_rate`, `errors=0`) do not count.
const CAUSE_MARKERS: [&str; 8] = [
    "ERROR",
    "Error",
    "Exception",
    "Abort",
    "CUDA error",
    "assert",
    "FATAL",
    "Fatal",
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

/// Characters of an uncaught exception carried in the exit summary.  The
/// summary crosses bounded to 480 characters; this leaves room for the exit
/// facts in front of it.
const EXCEPTION_CHARS: usize = 300;

/// Whether a line is a traceback frame: `File "...", line N, in f`.
fn is_frame(line: &[u8]) -> bool {
    line_content(line).trim_start().starts_with("File \"")
}

/// The line that closes the last complete Python traceback: its start and end.
///
/// A traceback is recognised by its opener or by its frames, because a bounded
/// window may hold the end of a traceback without the line that opened it.  The
/// closing line is the first one after the frames that does not continue them,
/// when it names an exception class.
fn closing_exception(stream: &[u8]) -> Option<(usize, usize)> {
    let mut in_traceback = false;
    let mut found = None;
    let mut offset = 0;
    for line in stream.split_inclusive(|byte| *byte == b'\n') {
        let start = offset;
        offset += line.len();
        if contains(line, TRACEBACK) || is_frame(line) {
            in_traceback = true;
        } else if in_traceback && !continues_traceback(line) {
            if names_an_exception(line) {
                found = Some((start, offset));
            }
            in_traceback = false;
        }
    }
    found
}

/// The start of the last frame above `before`, when it lies within half the
/// budget: the frame that raised the exception closing at `before`.
fn raising_frame(stream: &[u8], before: usize) -> Option<usize> {
    let mut offset = 0;
    let mut frame = None;
    for line in stream[..before].split_inclusive(|byte| *byte == b'\n') {
        if is_frame(line) {
            frame = Some(offset);
        }
        offset += line.len();
    }
    frame.filter(|start| before - start <= RETAINED_BYTES / 2)
}

/// Where a window that ends on an exception line ends instead: on the stream's
/// last line when the budget reaches it, otherwise on the last whole line the
/// budget holds, counted from the frame that raised the exception.  An
/// exception line longer than the budget ends the window as it did before.
fn through_the_message(stream: &[u8], exception_start: usize, exception_end: usize) -> usize {
    let from = raising_frame(stream, exception_start).unwrap_or(exception_start);
    let limit = (from + RETAINED_BYTES).clamp(exception_end, stream.len());
    if limit == stream.len() {
        return limit;
    }
    stream[exception_end..limit]
        .iter()
        .rposition(|byte| *byte == b'\n')
        .map_or(exception_end, |index| exception_end + index + 1)
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
/// no reporting block keeps its newest bytes.  A window that would end on a
/// traceback's closing exception runs on past it, so a message printed over
/// several lines is kept whole.
///
/// A block that points above itself with no traceback above it still has a
/// cause: the lines its processes reported as errors before it.  Those come
/// back as separate line ranges, retained ahead of the window.
fn window(stream: &[u8]) -> (Vec<Range<usize>>, Range<usize>) {
    let Some((header_start, header_end, block_start, cause_above)) = last_failure_line(stream)
    else {
        return (
            Vec::new(),
            stream.len().saturating_sub(RETAINED_BYTES)..stream.len(),
        );
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
    let points_above = SELF_REFERENTIAL
        .iter()
        .any(|marker| contains_ascii_case(&stream[block_start..header_end], marker));
    if header_start > block_start && cause_above.is_some() && points_above {
        return (
            Vec::new(),
            block_start.saturating_sub(RETAINED_BYTES)..block_start,
        );
    }
    let end = match closing_exception(stream) {
        Some((exception_start, exception_end)) if exception_end == header_end => {
            through_the_message(stream, exception_start, exception_end)
        }
        _ => header_end,
    };
    let causes = if points_above && cause_above.is_none() {
        first_failure_lines(stream, block_start, RETAINED_BYTES / 2)
    } else {
        Vec::new()
    };
    let kept: usize = causes.iter().map(Range::len).sum();
    let start = end.saturating_sub(RETAINED_BYTES - kept - OMISSION_MARKER_BYTES);
    let causes: Vec<_> = causes
        .into_iter()
        .filter(|line| line.end <= start)
        .collect();
    if causes.is_empty() {
        return (Vec::new(), end.saturating_sub(RETAINED_BYTES)..end);
    }
    (causes, start..end)
}

/// The lines above `before` that report a failure, within `budget` bytes and in
/// stream order: the first such line of every process (its `(Name pid=N)`
/// prefix) before any process's later ones, so one noisy process cannot crowd
/// out the line another printed when it failed.
fn first_failure_lines(stream: &[u8], before: usize, budget: usize) -> Vec<Range<usize>> {
    let mut offset = 0;
    let mut processes: Vec<&[u8]> = Vec::new();
    let (mut first, mut later) = (Vec::new(), Vec::new());
    for line in stream[..before].split_inclusive(|byte| *byte == b'\n') {
        let range = offset..offset + line.len();
        offset = range.end;
        if !CAUSE_MARKERS
            .iter()
            .any(|marker| contains(line, marker.as_bytes()))
        {
            continue;
        }
        let process = if line.starts_with(b"(") {
            line.split_inclusive(|byte| *byte == b')')
                .next()
                .unwrap_or_default()
        } else {
            &[][..]
        };
        if processes.contains(&process) {
            later.push(range);
        } else {
            processes.push(process);
            first.push(range);
        }
    }
    let mut used = 0;
    let mut kept: Vec<_> = first
        .into_iter()
        .chain(later)
        .filter(|line| {
            let fits = used + line.len() <= budget;
            used += if fits { line.len() } else { 0 };
            fits
        })
        .collect();
    kept.sort_by_key(|line| line.start);
    kept
}

/// Retain one stream's window without breaking a line in half, and report what
/// was dropped so a window can never be mistaken for the whole log.  Failure
/// lines retained from above the window come first, followed by a line that
/// marks what was omitted between them and the window.
pub fn retain(stream: &[u8]) -> FailureLogTail {
    let (causes, range) = window(stream);
    let (start, length) = (range.start, range.len());
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
    let kept = causes.iter().map(Range::len).sum::<usize>() + selected.len();
    let dropped = (stream.len() - kept) as u64;
    let mut text = String::new();
    for line in &causes {
        text.push_str(&String::from_utf8_lossy(&stream[line.clone()]));
    }
    if !causes.is_empty() {
        text.push_str(&omission_marker(dropped));
        text.push('\n');
    }
    let tail = String::from_utf8_lossy(selected);
    let retained_lines = causes.len() + tail.lines().count();
    text.push_str(&tail);
    let total_lines = stream.iter().filter(|byte| **byte == b'\n').count();
    FailureLogTail {
        text,
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
    pub host_memory: Option<vonk_agent_protocol::failure_evidence::FailureDiagnostics>,
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
        host_memory: None,
    })
}

/// Why a container exited, as a stable token the Controller can show and group.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExitCause {
    OomKilled,
    HostMemoryExhausted,
    BadArguments,
    MissingFile,
    UnsupportedGpuArch,
    ImageArchMismatch,
    StartRefused,
    CrashedBySignal,
    PythonException,
    Unclassified,
}

impl ExitCause {
    pub fn token(self) -> &'static str {
        match self {
            Self::OomKilled => "oom_killed",
            Self::HostMemoryExhausted => "host_memory_exhausted",
            Self::BadArguments => "bad_arguments",
            Self::MissingFile => "missing_file",
            Self::UnsupportedGpuArch => "unsupported_gpu_arch",
            Self::ImageArchMismatch => "image_arch_mismatch",
            Self::StartRefused => "start_refused",
            Self::CrashedBySignal => "crashed_by_signal",
            Self::PythonException => "python_exception",
            Self::Unclassified => "unclassified",
        }
    }
}

/// The uncaught exception a workload printed last, as one bounded line: the
/// closing `<ExceptionType>: <message>` line of its last complete traceback and
/// the lines its message continues on.  stderr is read first because that is
/// where Python writes a traceback.
pub fn python_exception(logs: &HostHelperProcessLogs) -> Option<String> {
    [&logs.stderr.text, &logs.stdout.text]
        .into_iter()
        .find_map(|text| {
            let stream = text.as_bytes();
            let (start, end) = closing_exception(stream)?;
            let mut message = line_content(&stream[start..end]).trim().to_owned();
            for line in stream[end..].split_inclusive(|byte| *byte == b'\n') {
                let content = line_content(line);
                let content = content.trim();
                // The message ends where other output begins: a blank line, a
                // timestamped or prefixed log line, a new traceback, or a line
                // that names its own class (`INFO: ...`, a chained exception).
                if content.is_empty()
                    || content.starts_with('[')
                    || contains(line, TRACEBACK)
                    || is_frame(line)
                    || names_an_exception(line)
                    || message.chars().count() >= EXCEPTION_CHARS
                {
                    break;
                }
                message.push(' ');
                message.push_str(content);
            }
            Some(message.chars().take(EXCEPTION_CHARS).collect())
        })
}

fn has(text: &str, needles: &[&str]) -> bool {
    needles.iter().any(|needle| text.contains(needle))
}

/// Classify from the exit state first (it is authoritative), then from the
/// container's own words.  Only the retained tails are read.
pub fn classify(exit: Option<&ContainerExit>, logs: Option<&HostHelperProcessLogs>) -> ExitCause {
    if exit.is_some_and(|exit| exit.host_memory.is_some()) {
        return ExitCause::HostMemoryExhausted;
    }
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
    // An uncaught exception is the workload's own account of why it stopped.
    // It ranks below the specific causes above, which an exception often
    // carries (`FileNotFoundError: ... No such file or directory`), and above
    // the bare signal, which is how a process that already reported its
    // exception may still end.
    if logs.and_then(python_exception).is_some() {
        return ExitCause::PythonException;
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
    if let Ok(state) = exit
        && let Some(evidence) = &state.host_memory
    {
        return crate::host_memory_guard::summary(evidence);
    }
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
    let mut summary = parts.join(" ");
    // The exception is the summary's last field: it is the workload's own free
    // text, so a reader takes the facts before it as tokens.  It is carried for
    // every cause, because a named cause such as `missing_file` still reads
    // better with the path the exception names.
    if let Some(exception) = logs.and_then(python_exception) {
        summary.push_str(vonk_agent_protocol::failure_evidence::EXIT_SUMMARY_EXCEPTION_FIELD);
        summary.push_str(&format!("{exception:?}"));
    }
    summary
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
        stream.extend(b"(EngineCore pid=178) ERROR 10-15 11:33:50 [core.py:708] EngineCore failed to start.\n");
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
            host_memory: None,
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

    /// The live shape: sglang refusing a model's attention backend at start.
    /// The frames alone exceed the budget, and the exception's message runs on
    /// over the lines below it.
    fn sglang_backend_refusal() -> Vec<u8> {
        let mut stream =
            b"[2026-10-15 08:12:00] server_args=ServerArgs(model_path='/models/gpt-oss')\n"
                .to_vec();
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
        .take(24)
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
        let output = logs("", std::str::from_utf8(&sglang_backend_refusal()).unwrap());
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

    #[test]
    fn an_exception_message_is_bounded() {
        let mut stream =
            b"Traceback (most recent call last):\n  File \"x.py\", line 1, in f\n".to_vec();
        stream.extend(format!("ValueError: {}\n", "x ".repeat(400)).into_bytes());
        let exception = python_exception(&logs("", std::str::from_utf8(&stream).unwrap()))
            .expect("an exception");
        assert!(exception.starts_with("ValueError: x x"), "{exception}");
        assert_eq!(exception.chars().count(), EXCEPTION_CHARS);
        // An exception line longer than the whole budget still yields a
        // bounded window rather than a slice past the line's end.
        stream.extend(format!("ValueError: {}\n", "x ".repeat(2000)).into_bytes());
        let tail = retain(&stream);
        assert!(tail.text.len() <= RETAINED_BYTES);
        assert!(tail.truncated);
    }

    #[test]
    fn a_named_cause_keeps_its_name_and_carries_the_exception() {
        let stderr = "Traceback (most recent call last):\n  File \"/app/load.py\", line 3, in load\n    open(path)\nFileNotFoundError: [Errno 2] No such file or directory: '/models/x.gguf'\n";
        let state = exit(1, false, "");
        let output = logs("", stderr);
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
        let output = logs(
            "INFO: Started server process\nWARNING: low memory\n",
            "Error: model not ready\n",
        );
        let state = exit(3, false, "");
        assert_eq!(
            classify(Some(&state), Some(&output)),
            ExitCause::Unclassified
        );
        assert!(python_exception(&output).is_none());
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
                host_memory: None,
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
