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
//! * The window keeps the memory figures.  A workload that runs out of memory
//!   while starting (vLLM's "No available memory for the cache blocks") said
//!   how much it found and took a few lines before its traceback: "Model
//!   loading took 64.98 GiB memory", "Available KV cache memory: 0.46 GiB".
//!   The traceback fills the window, so the window is chosen within a smaller
//!   budget and those lines are retained ahead of it in the same way, and the
//!   fix can be sized from the evidence.
//! * The window keeps a native abort's message.  A C++ exception that ends the
//!   process (`PyTorch`'s `c10::Error`) is reported as `terminate called ...`
//!   and `what(): <message>`, then a long native stack and often a wrapper's
//!   traceback.  The message lines are retained ahead of the tail, with the
//!   omitted middle marked, so the evidence keeps both why the process aborted
//!   and how it ended.
//! * The window keeps the first error.  A failed start prints its root error
//!   first and later output repeats the failure (four identical tracebacks
//!   filled a window and hid the first).  When the window drops the head of a
//!   stream, each process's first error line, up to a quarter of the bound,
//!   are retained ahead of it, with the omitted middle marked.  They are
//!   evidence for the reader only: an exit is classified from the window
//!   without them, since the workload may have recovered from that error.

use std::ops::Range;
pub use vonk_agent_protocol::failure_evidence::ExitCause;
use vonk_agent_protocol::failure_evidence::{EXIT_CAUSE_FIELD, omission_marker};
use vonk_agent_protocol::generated::{FailureLogTail, HostHelperProcessLogs};

/// Bytes retained per container stream: the protocol's bound for this field.
///
/// Every later reader keeps it whole: the agent and the Controller each bound
/// the text to the same bound after redaction, shrinking the window and keeping the
/// causes ahead of the omission marker, and the evidence bundle and the
/// operator log projection carry it as it arrives.
const RETAINED_BYTES: usize = vonk_agent_protocol::failure_evidence::LOG_TAIL_BYTES;
/// Bytes reserved for the line that marks the output omitted between the
/// retained failure lines and the tail.
const OMISSION_MARKER_BYTES: usize = 48;
/// Bytes of memory-figure lines retained ahead of the window, reserved from
/// the window's budget so the stream's bound stays [`RETAINED_BYTES`].
const MEMORY_FIGURE_BYTES: usize = RETAINED_BYTES / 4;
/// Bytes of the stream's first lines retained ahead of the window when the
/// window drops the head, reserved from the window's budget.
const HEAD_BYTES: usize = RETAINED_BYTES / 4;
/// Lines read back from the container.  Kept well above what is retained so a
/// failure header above the retained window is still visible to the anchor.
/// The capture is the smaller of these lines and the last
/// `package_command::STREAM_RETAINED_BYTES` (32 KiB) of each stream, which is
/// all the helper's command runner keeps of `docker logs`.
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

/// The imperative that sends a reader elsewhere for a failure's cause, and
/// what it sends them to: a place (`above`, `earlier`) or a record that holds
/// a cause (`root cause`, `stack trace`).  Matched as whole lowercase words.
const POINTERS: [&str; 3] = ["see", "check", "refer"];
///
/// `previous` is not a place: it qualifies another noun far more often than it
/// points back (vLLM's "check the difference ... between the current run and
/// the previous run", sglang's "since previous check").
const TARGETS: [&str; 7] = [
    "above",
    "earlier",
    "preceding",
    "root cause",
    "trace",
    "traceback",
    "stacktrace",
];

/// Whether a line names itself as a summary of a cause printed elsewhere.
///
/// vLLM ends a worker failure with "See stack trace for root cause.", its
/// engine with "See root cause above." and its executor with "please check
/// the stack trace above for the root cause"; sglang, torch and others word the
/// same pointer their own way.  The decision is therefore by shape: a pointer
/// followed, in the same sentence, by a place or a record.  The order is what
/// separates a pointer from a cause that uses the same words: sglang's
/// `kernel_run_counter did not increase since previous check` and `Trace file
/// integrity check failed` put the place or record before `check`, and
/// `ValueError: 8192 is above the limit` has no pointer at all.
fn points_elsewhere(line: &[u8]) -> bool {
    let text = String::from_utf8_lossy(line).to_lowercase();
    text.split(['.', ';', '!', '?']).any(|sentence| {
        let words: Vec<&str> = sentence
            .split(|character: char| !(character.is_alphanumeric() || character == '_'))
            .filter(|word| !word.is_empty())
            .collect();
        words
            .iter()
            .position(|word| POINTERS.contains(word))
            .is_some_and(|pointer| {
                let after = format!(" {} ", words[pointer + 1..].join(" "));
                TARGETS
                    .iter()
                    .any(|target| after.contains(&format!(" {target} ")))
            })
    })
}

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
#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
fn line_content(line: &[u8]) -> String {
    let text = String::from_utf8_lossy(line);
    let mut rest: &str = text.trim_end_matches(['\r', '\n']);
    // Only a `(Name pid=N) ` opener is a process prefix; a message line such as
    // `(4, 8) vs (8, 4)` is content.
    if rest.starts_with('(')
        && let Some(index) = rest.find(") ")
        && rest[..index].contains(" pid=")
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
/// `budget`: the frame that raised the exception closing at `before`.
fn raising_frame(stream: &[u8], before: usize, budget: usize) -> Option<usize> {
    let mut offset = 0;
    let mut frame = None;
    for line in stream[..before].split_inclusive(|byte| *byte == b'\n') {
        if is_frame(line) {
            frame = Some(offset);
        }
        offset += line.len();
    }
    frame.filter(|start| before - start <= budget / 2)
}

/// Where a window that ends on an exception line ends instead: on the stream's
/// last line when the budget reaches it, otherwise on the last whole line the
/// budget holds, counted from the frame that raised the exception.  An
/// exception line longer than the budget ends the window as it did before.
fn through_the_message(
    stream: &[u8],
    exception_start: usize,
    exception_end: usize,
    budget: usize,
) -> usize {
    let from = raising_frame(stream, exception_start, budget).unwrap_or(exception_start);
    let limit = (from + budget).clamp(exception_end, stream.len());
    if limit == stream.len() {
        return limit;
    }
    stream[exception_end..limit]
        .iter()
        .rposition(|byte| *byte == b'\n')
        .map_or(exception_end, |index| exception_end + index + 1)
}

/// The retained byte range of one stream and the lines retained ahead of it:
/// the [`failure_window`], with the memory figures printed above it.
///
/// A stream with no figure above its window keeps that window.  Otherwise the
/// window is chosen again within a budget that leaves room for the figures
/// and the omission marker, so every anchor of the window -- the raising
/// frame, the exception, the causes -- is placed within the smaller budget
/// rather than cut from its top afterwards, and the figures are taken from
/// above that smaller window.
fn window_within(stream: &[u8], total: usize) -> (Vec<Range<usize>>, Range<usize>) {
    let (causes, range) = failure_window(stream, total);
    if memory_figures(
        &stream[..above(stream, &range)],
        &causes,
        MEMORY_FIGURE_BYTES,
    )
    .is_empty()
    {
        return (causes, range);
    }
    let budget = total - MEMORY_FIGURE_BYTES - OMISSION_MARKER_BYTES;
    let (reduced_causes, reduced) = failure_window(stream, budget);
    let figures = memory_figures(
        &stream[..above(stream, &reduced)],
        &reduced_causes,
        MEMORY_FIGURE_BYTES,
    );
    if figures.is_empty() {
        return (causes, range);
    }
    let mut retained: Vec<_> = reduced_causes.into_iter().chain(figures).collect();
    retained.sort_by_key(|line| line.start);
    (retained, reduced)
}

/// The window with the stream's first lines kept ahead of it.
///
/// The first error of a failed start is usually printed first, and a window
/// anchored on the last failure line (or a plain tail) loses it when later
/// output repeats the same failure.  When the window drops the head of the
/// stream, the each process's first error line, up to [`HEAD_BYTES`], is retained as causes,
/// the window is chosen again within what they and the omission marker leave,
/// and everything retained still fits in [`RETAINED_BYTES`].
fn window(stream: &[u8], keep_head: bool) -> (Vec<Range<usize>>, Range<usize>) {
    let first = window_within(stream, RETAINED_BYTES);
    let covered = first.0.first().map_or(first.1.start, |line| line.start);
    if !keep_head || covered == 0 {
        return first;
    }
    let (mut causes, range) =
        window_within(stream, RETAINED_BYTES - HEAD_BYTES - OMISSION_MARKER_BYTES);
    let below = causes
        .first()
        .map_or(above(stream, &range), |line| line.start);
    // The first error line of each process, as for a summary that points above.
    let mut head = first_failure_lines(stream, below, HEAD_BYTES);
    if head.is_empty() {
        return first;
    }
    head.append(&mut causes);
    (head, range)
}

/// Where the bytes above `window` end: at its start, or past the line it opens
/// inside, which [`retain`] drops from the tail as partial.
fn above(stream: &[u8], window: &Range<usize>) -> usize {
    if window.start == 0 || stream[window.start - 1] == b'\n' {
        return window.start;
    }
    stream[window.clone()]
        .iter()
        .position(|byte| *byte == b'\n')
        .map_or(window.start, |index| window.start + index + 1)
}

/// The byte range that ends on the stream's own account of its failure.
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
///
/// Everything retained fits in `budget` bytes.
fn failure_window(stream: &[u8], budget: usize) -> (Vec<Range<usize>>, Range<usize>) {
    let Some((header_start, header_end, block_start, cause_above)) = last_failure_line(stream)
    else {
        let end = stream.len();
        return ahead_of_window(stream, native_abort(stream, end, budget / 2), end, budget);
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
    let points_above = stream[block_start..header_end]
        .split_inclusive(|byte| *byte == b'\n')
        .any(points_elsewhere);
    if header_start > block_start && cause_above.is_some() && points_above {
        return (Vec::new(), block_start.saturating_sub(budget)..block_start);
    }
    let end = match closing_exception(stream) {
        Some((exception_start, exception_end)) if exception_end == header_end => {
            through_the_message(stream, exception_start, exception_end, budget)
        }
        _ => header_end,
    };
    let causes = if points_above && cause_above.is_none() {
        first_failure_lines(stream, block_start, budget / 2)
    } else {
        native_abort(stream, end, budget / 2)
    };
    ahead_of_window(stream, causes, end, budget)
}

/// The window ending at `end`, with `causes` retained ahead of it when they lie
/// above it: the window gives up the bytes the causes and the omission marker
/// take, so everything retained still fits in `budget`.  Causes the window
/// already holds are not repeated, and with none left the window keeps the
/// whole budget.
fn ahead_of_window(
    stream: &[u8],
    causes: Vec<Range<usize>>,
    end: usize,
    budget: usize,
) -> (Vec<Range<usize>>, Range<usize>) {
    let kept: usize = causes.iter().map(Range::len).sum();
    let start = end.saturating_sub(budget - kept - OMISSION_MARKER_BYTES);
    let causes: Vec<_> = causes
        .into_iter()
        .filter(|line| line.end <= above(stream, &(start..end)))
        .collect();
    if causes.is_empty() {
        return (Vec::new(), end.saturating_sub(budget)..end);
    }
    (causes, start..end)
}

/// How a C++ runtime reports the exception that aborts its process
/// (libstdc++'s terminate handler, which `PyTorch`'s `c10::Error` goes through):
/// `terminate called after throwing an instance of 'c10::Error'`, then
/// `  what():  <message>` over as many lines as the message has.  The stack it
/// prints next (`Exception raised from ...`, `frame #0: ...`) is long, and any
/// wrapper's own traceback follows it, so a window at the end keeps frames and
/// loses the message.
const NATIVE_ABORT: &[u8] = b"terminate called ";
/// The opener that a `what()` message follows.  The handler's other reports
/// (`terminate called without an active exception`, `... recursively`) are one
/// line each.
const NATIVE_ABORT_THROWN: &[u8] = b"terminate called after throwing";

/// The last native abort report above `before`: its opening line and, for a
/// thrown exception, the `what()` message lines after it, whole lines within
/// `budget` bytes.
fn native_abort(stream: &[u8], before: usize, budget: usize) -> Vec<Range<usize>> {
    let above = &stream[..before];
    let mut offset = 0;
    let mut opener = None;
    for line in above.split_inclusive(|byte| *byte == b'\n') {
        if contains(line, NATIVE_ABORT) {
            opener = Some(offset);
        }
        offset += line.len();
    }
    let Some(opener) = opener else {
        return Vec::new();
    };
    let mut kept = Vec::new();
    let mut used = 0;
    let mut offset = opener;
    for line in above[opener..].split_inclusive(|byte| *byte == b'\n') {
        let range = offset..offset + line.len();
        offset = range.end;
        let content = line_content(line);
        let content = content.trim();
        // The message ends where the stack, a blank line or another report
        // begins.
        let ends = !kept.is_empty()
            && (content.is_empty()
                || content.starts_with("Exception raised from")
                || content.starts_with("frame #")
                || contains(line, NATIVE_ABORT)
                || contains(line, TRACEBACK));
        if ends || !line.ends_with(b"\n") || used + line.len() > budget {
            break;
        }
        used += line.len();
        kept.push(range);
        if kept.len() == 1 && !contains(line, NATIVE_ABORT_THROWN) {
            break;
        }
    }
    kept
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

/// Words that say a byte quantity on the same line is memory, matched as whole
/// lowercase words: vLLM's "Model loading took 64.98 GiB memory" and
/// "Available KV cache memory: 0.46 GiB", sglang's "avail mem=116.04 GB" and
/// "KV Cache is allocated. ... K size: 5.89 GB", "Available RAM: 49.31 GiB".
const MEMORY_WORDS: [&str; 4] = ["memory", "mem", "ram", "cache"];
/// Units of a byte quantity, written after a number with at most one space.
const BYTE_UNITS: [&str; 4] = ["GiB", "GB", "MiB", "MB"];

/// Whether a line reports a memory figure: it names memory and carries a byte
/// quantity.  A download's size (`Downloading apache-tvm-ffi (2.4 MB)`) names
/// no memory, and a setting (`gpu_memory_utilization=0.9`) carries no byte
/// quantity.
#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
fn reports_memory(line: &[u8]) -> bool {
    let text = String::from_utf8_lossy(line);
    let names_memory = text
        .split(|character: char| !character.is_ascii_alphanumeric())
        .any(|word| MEMORY_WORDS.contains(&word.to_ascii_lowercase().as_str()));
    names_memory
        && BYTE_UNITS.iter().any(|unit| {
            text.match_indices(unit).any(|(index, _)| {
                let before = &text[..index];
                let before = before.strip_suffix(' ').unwrap_or(before);
                before.ends_with(|character: char| character.is_ascii_digit())
                    && !text[index + unit.len()..]
                        .starts_with(|character: char| character.is_ascii_alphanumeric())
            })
        })
}

/// The whole lines of `above` that report a memory figure and are not already
/// `retained`, newest first within `budget` bytes, in stream order: the
/// figures nearest the failure describe the attempt that failed.
fn memory_figures(above: &[u8], retained: &[Range<usize>], budget: usize) -> Vec<Range<usize>> {
    let mut offset = 0;
    let mut figures = Vec::new();
    for line in above.split_inclusive(|byte| *byte == b'\n') {
        let range = offset..offset + line.len();
        offset = range.end;
        if line.ends_with(b"\n") && !retained.contains(&range) && reports_memory(line) {
            figures.push(range);
        }
    }
    let mut used = 0;
    let mut kept: Vec<_> = figures
        .into_iter()
        .rev()
        .filter(|line| {
            let fits = used + line.len() <= budget;
            used += if fits { line.len() } else { 0 };
            fits
        })
        .collect();
    kept.reverse();
    kept
}

/// Retain one stream's window without breaking a line in half, and report what
/// was dropped so a window can never be mistaken for the whole log.  Failure
/// lines retained from above the window come first, followed by a line that
/// marks what was omitted between them and the window.
pub fn retain(stream: &[u8]) -> FailureLogTail {
    retain_stream(stream, true)
}

/// [`retain`], with or without the stream's first error ahead of the window.
#[expect(
    clippy::naive_bytecount,
    reason = "small log streams; a counting dependency is not warranted"
)]
fn retain_stream(stream: &[u8], keep_head: bool) -> FailureLogTail {
    let (causes, range) = window(stream, keep_head);
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
        exit_code: None,
        stdout: retain(stdout),
        stderr: retain(stderr),
    }
}

/// The windows an exit is classified from: [`retain_container`] without the
/// first error of each stream.  That error is evidence for the reader; the
/// workload may have recovered from it, so it never names the exit.
fn classified_windows(stdout: &[u8], stderr: &[u8]) -> HostHelperProcessLogs {
    HostHelperProcessLogs {
        exit_code: None,
        stdout: retain_stream(stdout, false),
        stderr: retain_stream(stderr, false),
    }
}

/// One failed container's captured output: what `docker logs --tail
/// CAPTURE_LINES` printed, as the helper's command runner kept it (the last
/// `package_command::STREAM_RETAINED_BYTES` of each stream).  An exit is
/// classified from this alone, and everything it needs is derived once, here:
/// the windows that cross the protocol, the classified windows (the same
/// windows without the first error kept for the reader, which is all
/// classification reads), their closing exception, and the
/// closing exception of the whole capture, admitted only when a window points
/// above itself.  Classification can never be handed a window.
#[derive(Clone, Debug, PartialEq)]
pub struct CapturedOutput {
    retained: HostHelperProcessLogs,
    classified: HostHelperProcessLogs,
    window_exception: Option<String>,
    /// The cause a window's pointer ("please check the stack trace above")
    /// sends the reader to, when the window holds no exception of its own:
    /// the closing exception of the whole capture.  Without a pointer it is
    /// never read, so a traceback the workload logged and recovered from far
    /// above an unrelated exit cannot name that exit.
    dropped_exception: Option<String>,
}

impl CapturedOutput {
    pub fn new(stdout: &[u8], stderr: &[u8]) -> Self {
        let retained = retain_container(stdout, stderr);
        let classified = classified_windows(stdout, stderr);
        let window_exception = python_exception(&classified);
        let points_above = [&classified.stdout.text, &classified.stderr.text]
            .into_iter()
            .any(|text| {
                text.as_bytes()
                    .split_inclusive(|byte| *byte == b'\n')
                    .any(points_elsewhere)
            });
        let dropped_exception = if window_exception.is_none() && points_above {
            let whole = |stream: &[u8]| FailureLogTail {
                text: String::from_utf8_lossy(stream).into_owned(),
                truncated: false,
                dropped_bytes: Some(0),
                dropped_lines: Some(0),
            };
            python_exception(&HostHelperProcessLogs {
                exit_code: None,
                stdout: whole(stdout),
                stderr: whole(stderr),
            })
        } else {
            None
        };
        Self {
            retained,
            classified,
            window_exception,
            dropped_exception,
        }
    }

    /// The bounded windows the protocol carries.
    pub fn retained(&self) -> &HostHelperProcessLogs {
        &self.retained
    }

    /// The windows, handed to the protocol once the exit is classified.
    pub fn into_retained(self) -> HostHelperProcessLogs {
        self.retained
    }

    /// The exception the exit summary carries: the windows' own, else the one
    /// their pointer names.
    fn exception(&self) -> Option<&str> {
        self.window_exception
            .as_deref()
            .or(self.dropped_exception.as_deref())
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

/// The uncaught exception a workload printed last, as one bounded line: the
/// closing `<ExceptionType>: <message>` line of its last complete traceback and
/// the lines its message continues on.  stderr is read first because that is
/// where Python writes a traceback, unless its exception is a pointer that
/// quotes stdout's: vLLM logs a worker's traceback to stdout while its engine
/// core's uncaught pointer to it, quoting the worker's message, ends stderr.
pub fn python_exception(logs: &HostHelperProcessLogs) -> Option<String> {
    let found: Vec<(String, String)> = [&logs.stderr.text, &logs.stdout.text]
        .into_iter()
        .filter_map(|text| {
            let stream = text.as_bytes();
            let (start, end) = closing_exception(stream)?;
            let exception = &stream[start..end];
            let mut message = line_content(exception).trim().to_owned();
            for line in stream[end..].split_inclusive(|byte| *byte == b'\n') {
                let content = line_content(line);
                let content = content.trim();
                // The message ends where other output begins: a blank line, a
                // timestamped log line, a line another process or log record
                // wrote, a new traceback, or a line that names its own class
                // (`INFO: ...`, a chained exception).
                if content.is_empty()
                    || content.starts_with('[')
                    || line_prefix(line) != line_prefix(exception)
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
            Some((
                message.chars().take(EXCEPTION_CHARS).collect::<String>(),
                line_content(exception),
            ))
        })
        .collect();
    let (message, line) = found.first()?;
    // A pointer that quotes another stream's exception (vLLM's "Worker failed
    // with error '<message>'") yields to that exception; any other exception
    // on that stream is unrelated output and the pointer stands.  The quotes
    // are part of the match, so a one-token message (`KeyError: 0`) cannot be
    // found inside the pointer's other text.
    let named = points_elsewhere(line.as_bytes())
        .then(|| {
            found[1..].iter().find(|(_, other)| {
                other.split_once(": ").is_some_and(|(_, said)| {
                    let said = said.trim();
                    !said.is_empty()
                        && (line.contains(&format!("'{said}'"))
                            || line.contains(&format!("\"{said}\"")))
                })
            })
        })
        .flatten();
    Some(named.map_or(message, |(other, _)| other).clone())
}

/// What `line_content` strips from a line: the process and log-record prefix.
#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
fn line_prefix(line: &[u8]) -> String {
    let text = String::from_utf8_lossy(line);
    let text = text.trim_end_matches(['\r', '\n']);
    text[..text.len() - line_content(line).len()].to_owned()
}

fn has(text: &str, needles: &[&str]) -> bool {
    needles.iter().any(|needle| text.contains(needle))
}

/// Classify from the exit state first (it is authoritative), then from the
/// container's own words in the classified windows (the retained windows
/// without the first error kept for the reader, not the text that crosses the
/// protocol).  A window can drop the line that names a cause: a two-Spark vLLM
/// start ends in kilobytes of C++ frames below the engine's `RuntimeError:
/// Worker failed with error 'CUDA error: no kernel image ...`, and the window
/// keeps only the frames and the pointer to it.  So an exit the windows leave
/// unclassified is read once more from the closing exception of the whole
/// capture, admitted only when the window points above; never from an
/// incidental line, nor from a traceback with no pointer to it.
pub fn classify(exit: Option<&ContainerExit>, output: Option<&CapturedOutput>) -> ExitCause {
    let cause = classify_retained(
        exit,
        output.map(|output| &output.classified),
        output.is_some_and(|output| output.window_exception.is_some()),
    );
    if cause != ExitCause::Unclassified {
        return cause;
    }
    match output.and_then(|output| output.dropped_exception.as_deref()) {
        Some(exception) => {
            named_cause(&exception.to_lowercase()).unwrap_or(ExitCause::PythonException)
        }
        None => cause,
    }
}

/// The specific cause a workload's own words name, most specific first.
fn named_cause(output: &str) -> Option<ExitCause> {
    if has(
        output,
        &[
            "no kernel image is available",
            "nokernelimagefordevice",
            "unsupported gpu architecture",
            "invalid device function",
        ],
    ) {
        return Some(ExitCause::UnsupportedGpuArch);
    }
    if has(
        output,
        &[
            "unknown argument",
            "unrecognized argument",
            "unrecognized option",
            "invalid argument",
            "error: invalid",
            "usage:",
        ],
    ) {
        return Some(ExitCause::BadArguments);
    }
    if has(
        output,
        &[
            "no such file or directory",
            "failed to open",
            "file not found",
            "failed to load model",
            "error loading model",
        ],
    ) {
        return Some(ExitCause::MissingFile);
    }
    None
}

/// `classify` over the retained windows alone; `window_exception` says
/// whether they close on an uncaught exception.
fn classify_retained(
    exit: Option<&ContainerExit>,
    logs: Option<&HostHelperProcessLogs>,
    window_exception: bool,
) -> ExitCause {
    if exit.is_some_and(|exit| exit.host_memory.is_some()) {
        return ExitCause::HostMemoryExhausted;
    }
    let output = logs.map_or_default(|logs| {
        format!("{}\n{}", logs.stdout.text, logs.stderr.text).to_lowercase()
    });
    let runtime_error = exit.map_or_default(|e| e.error.to_lowercase());
    let code = exit.and_then(|e| e.exit_code);
    // Only the runtime's own OOM fact names this cause. A bare 137 is just a
    // SIGKILL (an engine killing its own process tree after an exception, the
    // host guard, `docker kill`), so it is read like the other signal exits.
    if exit.and_then(|e| e.oom_killed) == Some(true) {
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
    if let Some(cause) = named_cause(&output) {
        return cause;
    }
    // An uncaught exception is the workload's own account of why it stopped.
    // It ranks below the specific causes above, which an exception often
    // carries (`FileNotFoundError: ... No such file or directory`), and above
    // the bare signal, which is how a process that already reported its
    // exception may still end.
    if window_exception {
        return ExitCause::PythonException;
    }
    if matches!(code, Some(134 | 135 | 137 | 139)) {
        return ExitCause::CrashedBySignal;
    }
    ExitCause::Unclassified
}

/// The one-line, machine-readable account of an exited container.  It is never
/// empty: when the runtime could not be asked, that is itself the account, so a
/// process-exit failure always says either what happened or why that is unknown.
pub fn exit_summary(
    exit: Result<&ContainerExit, &'static str>,
    output: Option<&CapturedOutput>,
    log_error: Option<&'static str>,
) -> String {
    if let Ok(state) = exit
        && let Some(evidence) = &state.host_memory
    {
        return crate::host_memory_guard::summary(evidence);
    }
    let cause = classify(exit.ok(), output);
    let logs = output.map(CapturedOutput::retained);
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
    parts.push(format!("{EXIT_CAUSE_FIELD}={}", cause.token()));
    match (logs, log_error) {
        (Some(logs), _) if logs.stdout.text.is_empty() && logs.stderr.text.is_empty() => {
            parts.push("no_output=true".to_owned());
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
    if let Some(exception) = output.and_then(CapturedOutput::exception) {
        summary.push_str(vonk_agent_protocol::failure_evidence::EXIT_SUMMARY_EXCEPTION_FIELD);
        summary = format!("{summary}{exception:?}");
    }
    summary
}

/// The diagnostic an exited workload's rejection carries: its exit summary,
/// then why its output could not be read. A host-memory-guard summary is all
/// typed facts and keeps its own bound; every other summary is cut at
/// `EXIT_SUMMARY_CHARS`. The reason stays a separate token so the last fact
/// (`observed_at`) is never glued to it.
pub fn exited_diagnostic(exit_summary: &str, capture_error: Option<&str>) -> String {
    use vonk_agent_protocol::failure_evidence::{
        EXIT_SUMMARY_CHARS, ExitCause as Cause, HOST_MEMORY_SUMMARY_CHARS,
    };
    let bound = if Cause::of_summary(exit_summary) == Some(Cause::HostMemoryExhausted) {
        HOST_MEMORY_SUMMARY_CHARS
    } else {
        EXIT_SUMMARY_CHARS
    };
    match capture_error {
        Some(reason) => format!("{exit_summary} ; {reason}"),
        None => exit_summary.to_owned(),
    }
    .chars()
    .take(bound)
    .collect()
}

#[cfg(test)]
mod tests;
