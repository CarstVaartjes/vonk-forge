#![cfg(test)]
//! Which host settings a pass corrects, and which it leaves alone.
use super::*;
use std::sync::Arc;

/// systemd as `systemctl` answers it: `None` cannot be read, and every
/// command is recorded.
#[derive(Clone)]
struct Systemd {
    default_target: Arc<Mutex<Option<&'static str>>>,
    display_manager: Arc<Mutex<Option<&'static str>>>,
    clock_cap: Arc<Mutex<Option<&'static str>>>,
    stop_fails: bool,
    start_fails: bool,
    calls: Arc<Mutex<Vec<String>>>,
}

impl Systemd {
    fn host(default_target: Option<&'static str>, display_manager: Option<&'static str>) -> Self {
        Self {
            default_target: Arc::new(Mutex::new(default_target)),
            display_manager: Arc::new(Mutex::new(display_manager)),
            // The clock cap unit ran at boot unless a test says otherwise.
            clock_cap: Arc::new(Mutex::new(Some("active"))),
            stop_fails: false,
            start_fails: false,
            calls: Arc::default(),
        }
    }

    /// The commands that changed the host.
    fn corrections(&self) -> Vec<String> {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|call| !call.starts_with("get-default") && !call.starts_with("is-active"))
            .cloned()
            .collect()
    }
}

fn answer(success: bool, word: &str) -> Result<CommandOutput, String> {
    Ok(CommandOutput {
        success,
        stdout: format!("{word}\n").into_bytes(),
        stderr: if success {
            Vec::new()
        } else {
            b"denied".to_vec()
        },
        exit_code: Some(if success { 0 } else { 3 }),
    })
}

impl CommandRunner for Systemd {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        assert_eq!(executable, Path::new("/usr/bin/systemctl"));
        let call = arguments.join(" ");
        self.calls.lock().unwrap().push(call.clone());
        match call.as_str() {
            "get-default" => match *self.default_target.lock().unwrap() {
                Some(target) => answer(true, target),
                None => Err("bus unreachable".to_owned()),
            },
            "is-active display-manager.service" => match *self.display_manager.lock().unwrap() {
                Some(state) => answer(state == "active", state),
                None => Err("bus unreachable".to_owned()),
            },
            "set-default multi-user.target" => {
                *self.default_target.lock().unwrap() = Some("multi-user.target");
                answer(true, "")
            }
            "stop display-manager.service" if self.stop_fails => answer(false, ""),
            "stop display-manager.service" => {
                *self.display_manager.lock().unwrap() = Some("inactive");
                answer(true, "")
            }
            "is-active vonk-forge-gpu-clock-cap.service" => match *self.clock_cap.lock().unwrap() {
                Some(state) => answer(state == "active", state),
                None => Err("bus unreachable".to_owned()),
            },
            "start vonk-forge-gpu-clock-cap.service" if self.start_fails => {
                *self.clock_cap.lock().unwrap() = Some("failed");
                answer(false, "")
            }
            "start vonk-forge-gpu-clock-cap.service" => {
                *self.clock_cap.lock().unwrap() = Some("active");
                answer(true, "")
            }
            other => panic!("not a host-settings command: {other}"),
        }
    }
}

fn helper(systemd: &Systemd) -> (tempfile::TempDir, OperationExecutor<Systemd>) {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    fs::create_dir_all(&roots.data).unwrap();
    let executor = OperationExecutor::new(roots, &[0; 32], systemd.clone(), None).unwrap();
    (temp, executor)
}

#[test]
fn a_desktop_host_is_made_headless_and_a_second_pass_changes_nothing() {
    let systemd = Systemd::host(Some("graphical.target"), Some("active"));
    let (_temp, executor) = helper(&systemd);

    executor.keep_host_settings();
    assert_eq!(
        systemd.corrections(),
        [
            "set-default multi-user.target",
            "stop display-manager.service"
        ]
    );
    assert_eq!(
        *systemd.default_target.lock().unwrap(),
        Some("multi-user.target")
    );
    assert_eq!(*systemd.display_manager.lock().unwrap(), Some("inactive"));

    executor.keep_host_settings();
    assert_eq!(
        systemd.corrections().len(),
        2,
        "a headless host was changed"
    );
}

#[test]
fn a_failed_desktop_still_gets_the_headless_target_and_is_not_stopped() {
    // spark-2297: graphical.target whose display manager never came back.
    let systemd = Systemd::host(Some("graphical.target"), Some("failed"));
    let (_temp, executor) = helper(&systemd);

    executor.keep_host_settings();
    assert_eq!(systemd.corrections(), ["set-default multi-user.target"]);
}

#[test]
fn an_unreadable_setting_is_left_alone_and_the_other_is_still_kept() {
    let systemd = Systemd::host(None, Some("active"));
    let (_temp, executor) = helper(&systemd);
    executor.keep_host_settings();
    assert_eq!(systemd.corrections(), ["stop display-manager.service"]);

    let systemd = Systemd::host(Some("graphical.target"), None);
    let (_temp, executor) = helper(&systemd);
    executor.keep_host_settings();
    assert_eq!(systemd.corrections(), ["set-default multi-user.target"]);
}

#[test]
fn a_refused_stop_is_retried_on_the_next_pass() {
    let mut systemd = Systemd::host(Some("multi-user.target"), Some("active"));
    systemd.stop_fails = true;
    let (_temp, executor) = helper(&systemd);

    executor.keep_host_settings();
    executor.keep_host_settings();
    assert_eq!(
        systemd.corrections(),
        [
            "stop display-manager.service",
            "stop display-manager.service"
        ]
    );
}

#[test]
fn a_clock_cap_not_active_this_boot_is_started_once_and_a_refused_start_is_retried() {
    // A boot where the one-shot did not run, or ran before the driver was up.
    for before in ["inactive", "failed"] {
        let systemd = Systemd::host(Some("multi-user.target"), Some("inactive"));
        *systemd.clock_cap.lock().unwrap() = Some(before);
        let (_temp, executor) = helper(&systemd);
        executor.keep_host_settings();
        executor.keep_host_settings();
        assert_eq!(
            systemd.corrections(),
            ["start vonk-forge-gpu-clock-cap.service"]
        );
    }

    let mut systemd = Systemd::host(Some("multi-user.target"), Some("inactive"));
    *systemd.clock_cap.lock().unwrap() = Some("inactive");
    systemd.start_fails = true;
    let (_temp, executor) = helper(&systemd);
    executor.keep_host_settings();
    executor.keep_host_settings();
    assert_eq!(
        systemd.corrections(),
        [
            "start vonk-forge-gpu-clock-cap.service",
            "start vonk-forge-gpu-clock-cap.service"
        ]
    );

    // Unreadable: nothing is started, and the desktop settings are still kept.
    let systemd = Systemd::host(Some("graphical.target"), Some("inactive"));
    *systemd.clock_cap.lock().unwrap() = None;
    let (_temp, executor) = helper(&systemd);
    executor.keep_host_settings();
    assert_eq!(systemd.corrections(), ["set-default multi-user.target"]);
}
