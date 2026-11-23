use std::{env, os::unix::net::UnixDatagram, path::Path, sync::Mutex};

static RENEWAL_STATUS: Mutex<Option<String>> = Mutex::new(None);

/// Best-effort notification to the system service manager. Missing
/// NOTIFY_SOCKET is expected for foreground and test invocations.
pub fn notify(message: &str) {
    let Some(socket) = env::var_os("NOTIFY_SOCKET") else {
        return;
    };
    let socket = Path::new(&socket);
    // The packaged system unit uses systemd's filesystem notify socket. An
    // abstract address is intentionally ignored instead of guessing at an
    // unsafe or platform-specific path encoding.
    if !socket.is_absolute() {
        return;
    }
    let Ok(datagram) = UnixDatagram::unbound() else {
        return;
    };
    let _ = datagram.send_to(message.as_bytes(), socket);
}

pub fn progress(status: &str) {
    let renewal = RENEWAL_STATUS
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    let status = combined_status(status, renewal.as_deref());
    notify(&progress_payload(&status, watchdog_enabled()));
}

/// Retain renewal health when another lane updates systemd's single STATUS line.
pub fn renewal_status(status: Option<&str>) {
    *RENEWAL_STATUS
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = status.map(str::to_owned);
    progress("Agent certificate status");
}

fn combined_status(status: &str, renewal: Option<&str>) -> String {
    match renewal {
        Some(renewal) => format!("{status}; {renewal}"),
        None => status.to_owned(),
    }
}

/// Feed the service watchdog. The control loop calls this between steps, and a
/// running operation calls it on every accepted lease renewal: the unit's
/// `WatchdogSec` is shorter than a large model copy, so the loop alone cannot
/// keep a healthy agent alive through one.
pub fn watchdog() {
    if watchdog_enabled() {
        notify("WATCHDOG=1");
    }
}

pub fn watchdog_enabled() -> bool {
    watchdog_enabled_for(
        env::var("WATCHDOG_USEC").ok().as_deref(),
        env::var("WATCHDOG_PID").ok().as_deref(),
        std::process::id(),
    )
}

fn watchdog_enabled_for(usec: Option<&str>, pid: Option<&str>, process_id: u32) -> bool {
    usec.and_then(|value| value.parse::<u64>().ok())
        .is_some_and(|value| value > 0)
        && match pid {
            Some(value) => value.parse::<u32>().is_ok_and(|pid| pid == process_id),
            None => true,
        }
}

fn progress_payload(status: &str, watchdog: bool) -> String {
    if watchdog {
        format!("STATUS={status}\nWATCHDOG=1")
    } else {
        format!("STATUS={status}")
    }
}

#[cfg(test)]
mod tests;
