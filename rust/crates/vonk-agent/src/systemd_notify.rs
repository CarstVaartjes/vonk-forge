use std::{env, os::unix::net::UnixDatagram, path::Path};

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
    notify(&progress_payload(status, watchdog_enabled()));
}

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
mod tests {
    use super::{progress_payload, watchdog_enabled_for};

    #[test]
    fn watchdog_is_enabled_only_for_a_valid_current_process() {
        assert!(watchdog_enabled_for(Some("1000000"), None, 42));
        assert!(watchdog_enabled_for(Some("1000000"), Some("42"), 42));
        assert!(!watchdog_enabled_for(Some("1000000"), Some("1"), 42));
        assert!(!watchdog_enabled_for(Some("invalid"), None, 42));
        assert!(!watchdog_enabled_for(Some("0"), None, 42));
    }

    #[test]
    fn progress_pings_watchdog_only_when_service_manager_owns_this_process() {
        assert_eq!(
            progress_payload("loop progressed", true),
            "STATUS=loop progressed\nWATCHDOG=1"
        );
        assert_eq!(
            progress_payload("loop progressed", false),
            "STATUS=loop progressed"
        );
    }
}
