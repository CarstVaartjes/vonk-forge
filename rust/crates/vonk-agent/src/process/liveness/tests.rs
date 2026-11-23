#![cfg(test)]
use super::*;
use crate::process::{ProcessError, ProcessRunner, Program, SystemProcessRunner};
use std::{
    io::{Read, Write},
    net::TcpListener,
    thread,
};

const WINDOW: Duration = Duration::from_millis(600);
const CHUNKS: usize = 16;
const CHUNK: usize = 16 * 1024;
const REQUEST_LIMIT: usize = 8 * 1024;

/// Serve `chunks` body chunks, one per `pause`; zero chunks holds the
/// response open silently for several windows. The server is detached:
/// each process under test ends on its own body or stall window.
fn trickle(chunks: usize, pause: Duration) -> String {
    let listener = TcpListener::bind(crate::client::test_support::FIXTURE_BIND).unwrap();
    let url = format!("http://{}/", listener.local_addr().unwrap());
    thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        // Read the whole request first: closing a socket that still holds
        // unread request bytes sends a TCP RST, which can make curl fail
        // (exit 56) before it reads the body.
        let mut request = Vec::new();
        let mut byte = [0u8; 1];
        for _ in 0..REQUEST_LIMIT {
            if request.ends_with(b"\r\n\r\n") {
                break;
            }
            match stream.read(&mut byte) {
                Ok(1) => request.push(byte[0]),
                _ => return,
            }
        }
        let length = chunks.max(1) * CHUNK;
        let _ = write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {length}\r\n\r\n"
        );
        if chunks == 0 {
            thread::sleep(WINDOW * 4);
        }
        for _ in 0..chunks {
            thread::sleep(pause);
            if stream.write_all(&[b'x'; CHUNK]).is_err() {
                return;
            }
        }
    });
    url
}

fn curl(url: String) -> Vec<String> {
    vec!["--silent".into(), "--no-buffer".into(), url]
}

#[test]
fn output_keeps_a_process_alive_past_several_windows_and_counts_as_activity() {
    let observed = Arc::new(AtomicU64::new(0));
    let counter = observed.clone();
    let _scope = report_activity_to(Arc::new(move |amount| {
        counter.fetch_add(amount, Ordering::SeqCst);
    }));
    let url = trickle(CHUNKS, WINDOW / 4);
    let started = Instant::now();
    let output = SystemProcessRunner
        .run(Program::Curl, &curl(url), WINDOW)
        .unwrap();
    assert!(output.success);
    assert!(started.elapsed() > WINDOW * 3);
    assert!(observed.load(Ordering::SeqCst) >= (CHUNKS * CHUNK) as u64);
}

#[test]
fn sink_growth_keeps_a_process_alive_past_several_windows() {
    let url = trickle(CHUNKS, WINDOW / 4);
    let mut sink = tempfile::tempfile().unwrap();
    let started = Instant::now();
    let output = SystemProcessRunner
        .run_to_file(Program::Curl, &curl(url), WINDOW, &mut sink, u64::MAX)
        .unwrap();
    assert!(output.success);
    assert!(started.elapsed() > WINDOW * 3);
    assert_eq!(sink.metadata().unwrap().len(), (CHUNKS * CHUNK) as u64);
}

#[test]
fn a_silent_process_ends_after_one_window_and_a_fresh_one_runs() {
    for to_file in [false, true] {
        let url = trickle(0, WINDOW);
        let started = Instant::now();
        let result = if to_file {
            let mut sink = tempfile::tempfile().unwrap();
            SystemProcessRunner.run_to_file(Program::Curl, &curl(url), WINDOW, &mut sink, 1)
        } else {
            SystemProcessRunner.run(Program::Curl, &curl(url), WINDOW)
        };
        assert!(matches!(result, Err(ProcessError::Timeout)));
        assert!(started.elapsed() < WINDOW * 3);
    }
    let url = trickle(2, Duration::ZERO);
    assert!(
        SystemProcessRunner
            .run(Program::Curl, &curl(url), WINDOW)
            .unwrap()
            .success
    );
}
