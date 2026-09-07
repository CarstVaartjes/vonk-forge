//! Statically linked, offline scratch-image payload; never needs a shell.
#![forbid(unsafe_code)]
use std::{fs, process::ExitCode};

fn failure(code: u8) -> ExitCode {
    println!("vonk-runtime-preflight-error:{code}");
    ExitCode::from(code)
}

fn main() -> ExitCode {
    let status = match fs::read_to_string("/proc/self/status") {
        Ok(value) => value,
        Err(_) => return failure(21),
    };
    for key in ["CapEff:", "CapPrm:", "CapAmb:"] {
        if !status.lines().any(|line| {
            line.strip_prefix(key)
                .is_some_and(|value| value.trim() == "0000000000000000")
        }) {
            return failure(22);
        }
    }
    if !status.lines().any(|line| {
        line.strip_prefix("NoNewPrivs:")
            .is_some_and(|value| value.trim() == "1")
    }) {
        return failure(23);
    }
    if fs::read_link("/proc/self/ns/mnt").is_err() || fs::read_link("/proc/self/ns/user").is_err() {
        return failure(24);
    }
    let temporary = "/tmp/vonk-runtime-preflight";
    if fs::write(temporary, b"offline-preflight").is_err()
        || fs::read(temporary).ok().as_deref() != Some(b"offline-preflight")
    {
        return failure(25);
    }
    if fs::remove_file(temporary).is_err() {
        return failure(25);
    }
    println!("vonk-runtime-preflight-ok");
    ExitCode::SUCCESS
}
