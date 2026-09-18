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

/// Bytes retained per container stream.  This is the evidence the operator
/// receives, and the protocol declares the same bound, so this is the single
/// place the bound is applied.
const RETAINED_BYTES: usize = 2048;
/// Bytes retained before a failure header, so the cause printed above the
/// header survives alongside the head of the block that reports it.
const CAUSE_BYTES: usize = 1024;
/// Lines read back from the container.  Kept well above what is retained so a
/// failure header above the retained window is still visible to the anchor.
pub const CAPTURE_LINES: &str = "400";

/// Headers that open a block reporting why a process stopped.  A header is
/// matched at the start of its own line, so an ordinary mention of the word
/// `error` inside a message can never move the window.
const FAILURE_HEADERS: [&str; 11] = [
    "Traceback (most recent call last)",
    "RuntimeError:",
    "ValueError:",
    "ImportError:",
    "ModuleNotFoundError:",
    "KeyError:",
    "AssertionError:",
    "FATAL",
    "Fatal error",
    // The separator is part of the header so a configuration line such as
    // `ERROR_CODE=...` cannot move the window.
    "ERROR ",
    "ERROR:",
];

/// The last failure header in the capture, as a byte offset.
///
/// The scan is over the raw bytes: decoding first would make every line longer
/// than the bytes it came from whenever the container wrote a byte that is not
/// valid UTF-8, and an offset measured in decoded characters would then name a
/// position the stream does not have -- which is both a wrong anchor and a
/// panic in the slice below.
fn header_index(stream: &[u8]) -> Option<usize> {
    let mut found = None;
    let mut offset = 0;
    for line in stream.split_inclusive(|byte| *byte == b'\n') {
        let trimmed = line.strip_suffix(b"\n").unwrap_or(line);
        let trimmed = trimmed.strip_suffix(b"\r").unwrap_or(trimmed);
        if FAILURE_HEADERS
            .iter()
            .any(|header| trimmed.trim_ascii_start().starts_with(header.as_bytes()))
        {
            found = Some(offset);
        }
        offset += line.len();
    }
    found
}

/// The retained byte range of one stream: an offset and a length.
///
/// The last failure header is the anchor because it is the closest thing to the
/// failure itself wherever the capture stops: a complete traceback ends with its
/// exception, and a traceback cut off in mid-write is preceded by the cause the
/// frames were explaining.  Either way the retained window opens before the
/// anchor, not at the end of the log.
fn window(stream: &[u8]) -> (usize, usize) {
    if stream.len() <= RETAINED_BYTES {
        return (0, stream.len());
    }
    match header_index(stream) {
        Some(index) => (index.saturating_sub(CAUSE_BYTES), RETAINED_BYTES),
        None => (stream.len() - RETAINED_BYTES, RETAINED_BYTES),
    }
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
        let index = header_index(&stream).expect("the capture has a header");
        assert!(index < stream.len(), "{index} >= {}", stream.len());
        let tail = retain(&stream);
        assert!(tail.text.contains("drafter checkpoint is incompatible"));
        // The declared bound is characters. An undecodable byte becomes one
        // replacement character, so the window can never decode into more
        // characters than the bytes it selected; it can only weigh more bytes.
        assert!(tail.text.chars().count() <= RETAINED_BYTES);
        assert!(tail.text.len() <= RETAINED_BYTES * 3);
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
        let logs = retain_container(b"stdout before the failure\n", &cut_off_traceback());
        assert_eq!(logs.stdout.text, "stdout before the failure\n");
        assert!(logs.stderr.text.contains("from_vllm_config"));
    }

    #[test]
    fn an_empty_stream_is_reported_as_empty() {
        let tail = retain(&[]);
        assert_eq!(tail.text, "");
        assert_eq!(tail.dropped_bytes, Some(0));
        assert!(!tail.truncated);
    }
}
