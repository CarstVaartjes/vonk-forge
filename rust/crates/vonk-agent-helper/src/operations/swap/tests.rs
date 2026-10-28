//! A fake kernel (its /proc/swaps, MemAvailable, disk and the swap commands)
//! with real checkpoint files proves growth, release, refusal and recovery.
use super::*;
use std::sync::Arc;

const OS_SWAP: &str = "/swap.img";

#[derive(Default)]
struct State {
    active: Vec<ActiveSwap>,
    available_memory: u64,
    disk_available: u64,
    disk_total: u64,
    fail: Option<&'static str>,
    calls: Vec<(String, Vec<String>)>,
    boot: String,
    directory: PathBuf,
}

#[derive(Clone)]
struct Kernel(Arc<Mutex<State>>);

impl Kernel {
    fn new() -> Self {
        Self(Arc::new(Mutex::new(State {
            active: vec![ActiveSwap {
                path: PathBuf::from(OS_SWAP),
                size_bytes: 16 * GIB - header_bytes(),
                used_bytes: 0,
            }],
            available_memory: 100 * GIB,
            disk_available: 800 * GIB,
            disk_total: 4000 * GIB,
            boot: "boot-1".to_owned(),
            ..State::default()
        })))
    }
    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.0.lock().unwrap()
    }
    fn total(&self) -> u64 {
        self.state().active.iter().map(|swap| swap.size_bytes).sum()
    }
    fn commands(&self) -> Vec<String> {
        self.state()
            .calls
            .iter()
            .map(|(name, _)| name.clone())
            .collect()
    }
}

impl CommandRunner for Kernel {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        let executable = executable.to_str().unwrap().to_owned();
        // The fake enforces the same compiled argv as the process boundary.
        let directory = self.state().directory.clone();
        assert!(
            allowed_arguments(&directory, &executable, arguments),
            "{executable} {arguments:?}"
        );
        let mut state = self.state();
        state.calls.push((executable.clone(), arguments.to_vec()));
        if state.fail == Some(executable.as_str()) {
            return Ok(CommandOutput {
                success: false,
                stdout: vec![],
                stderr: b"injected failure".to_vec(),
                exit_code: Some(1),
            });
        }
        let path = PathBuf::from(arguments.last().unwrap());
        match executable.as_str() {
            FALLOCATE => {
                let bytes: u64 = arguments[1].parse().unwrap();
                fs::write(&path, bytes.to_string()).unwrap();
            }
            MKSWAP => assert!(path.exists()),
            SWAPON => {
                let bytes: u64 = fs::read_to_string(&path).unwrap().parse().unwrap();
                state.active.push(ActiveSwap {
                    path,
                    size_bytes: bytes - header_bytes(),
                    used_bytes: 0,
                });
            }
            // The kernel matches the file, not the path it prints.
            SWAPOFF => state
                .active
                .retain(|swap| swap.path.file_name() != path.file_name()),
            other => panic!("unexpected {other}"),
        }
        Ok(CommandOutput {
            success: true,
            stdout: vec![],
            stderr: vec![],
            exit_code: Some(0),
        })
    }

    fn read_host_text(&self, path: &Path) -> std::io::Result<String> {
        let state = self.state();
        Ok(match path.to_str().unwrap() {
            PROC_SWAPS => {
                let mut text = "Filename\tType\tSize\tUsed\tPriority\n".to_owned();
                for swap in &state.active {
                    text.push_str(&format!(
                        "{}\tfile\t{}\t{}\t-2\n",
                        swap.path.display(),
                        swap.size_bytes / 1024,
                        swap.used_bytes / 1024
                    ));
                }
                text
            }
            PROC_MEMINFO => format!("MemAvailable:   {} kB\n", state.available_memory / 1024),
            BOOT_ID => format!("{}\n", state.boot),
            other => panic!("unexpected read {other}"),
        })
    }

    fn filesystem_space(&self, _path: &Path) -> std::io::Result<FilesystemSpace> {
        let state = self.state();
        Ok(FilesystemSpace {
            available_bytes: state.disk_available,
            total_bytes: state.disk_total,
        })
    }
}

fn helper(root: &Path, kernel: &Kernel) -> OperationExecutor<Kernel> {
    kernel.state().directory = root.join(DIRECTORY);
    OperationExecutor::new(ManagedRoots::under(root), &[0; 32], kernel.clone(), None).unwrap()
}

fn intent(generation: u64, size_gib: u32) -> SwapIntent {
    SwapIntent {
        generation,
        size_gib,
    }
}

fn checkpoint(root: &Path) -> SwapCheckpoint {
    let bytes = fs::read(root.join(OBSERVATION_FILE)).unwrap();
    parse_strict(&bytes).unwrap()
}

fn managed_files(root: &Path) -> Vec<String> {
    let mut names: Vec<_> = fs::read_dir(root.join(DIRECTORY))
        .unwrap()
        .flatten()
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|name| managed_name(name))
        .collect();
    names.sort();
    names
}

#[test]
fn the_default_adds_one_file_so_all_swap_reaches_64_gib_and_leaves_the_os_swap_alone() {
    let temp = tempfile::tempdir().unwrap();
    let kernel = Kernel::new();
    let helper = helper(temp.path(), &kernel);
    helper.reconcile_swap_at(None, true, 1000).unwrap();
    assert_eq!(kernel.total(), 64 * GIB);
    assert_eq!(kernel.commands(), [FALLOCATE, MKSWAP, SWAPON]);
    assert!(
        kernel
            .state()
            .active
            .iter()
            .any(|swap| swap.path == Path::new(OS_SWAP))
    );
    let record = checkpoint(temp.path());
    assert_eq!(record.state, SwapApplyState::Applied);
    assert_eq!(record.desired, default_intent());
    assert_eq!(managed_files(temp.path()).len(), 1);
    // Converged: the next inventory and hourly passes change nothing.
    helper.reconcile_swap_at(None, false, 1001).unwrap();
    helper.reconcile_swap_at(None, true, 4600).unwrap();
    assert_eq!(kernel.commands().len(), 3);
}

#[test]
fn growth_activates_the_new_file_before_releasing_the_old_one() {
    let temp = tempfile::tempdir().unwrap();
    let kernel = Kernel::new();
    let helper = helper(temp.path(), &kernel);
    helper
        .reconcile_swap_at(Some(&intent(1, 64)), false, 1000)
        .unwrap();
    helper
        .reconcile_swap_at(Some(&intent(2, 96)), false, 1010)
        .unwrap();
    assert_eq!(kernel.total(), 96 * GIB);
    assert_eq!(kernel.commands()[3..], [FALLOCATE, MKSWAP, SWAPON, SWAPOFF]);
    assert_eq!(managed_files(temp.path()).len(), 1);
    assert_eq!(checkpoint(temp.path()).state, SwapApplyState::Applied);
}

#[test]
fn a_size_the_os_swap_already_meets_removes_the_managed_file() {
    let temp = tempfile::tempdir().unwrap();
    let kernel = Kernel::new();
    let helper = helper(temp.path(), &kernel);
    helper
        .reconcile_swap_at(Some(&intent(1, 64)), false, 1000)
        .unwrap();
    helper
        .reconcile_swap_at(Some(&intent(2, 16)), false, 1010)
        .unwrap();
    assert_eq!(kernel.total(), 16 * GIB - header_bytes());
    assert!(managed_files(temp.path()).is_empty());
    assert_eq!(checkpoint(temp.path()).state, SwapApplyState::Applied);
}

#[test]
fn a_shrink_while_a_workload_holds_its_pages_keeps_the_old_file_and_waits() {
    let temp = tempfile::tempdir().unwrap();
    let kernel = Kernel::new();
    let helper = helper(temp.path(), &kernel);
    helper
        .reconcile_swap_at(Some(&intent(1, 96)), false, 1000)
        .unwrap();
    {
        let mut state = kernel.state();
        let managed = state
            .active
            .iter_mut()
            .find(|swap| swap.path != Path::new(OS_SWAP))
            .unwrap();
        managed.used_bytes = 30 * GIB;
        state.available_memory = 40 * GIB;
    }
    helper
        .reconcile_swap_at(Some(&intent(2, 48)), false, 1010)
        .unwrap();
    // The new smaller file is active beside the old one: never less swap.
    assert!(kernel.total() >= 96 * GIB);
    assert!(!kernel.commands().contains(&SWAPOFF.to_owned()));
    let record = checkpoint(temp.path());
    assert_eq!(record.state, SwapApplyState::Waiting);
    assert_eq!(record.reason, Some(SwapWaitReason::ReleaseDeferred));
    assert_eq!(record.next_attempt_at, Some(1070));
    // Once the pages fit in memory with the release headroom to spare, it converges.
    kernel.state().available_memory = 60 * GIB;
    helper.reconcile_swap_at(None, false, 1070).unwrap();
    assert_eq!(kernel.total(), 48 * GIB);
    assert_eq!(checkpoint(temp.path()).state, SwapApplyState::Applied);
    assert_eq!(managed_files(temp.path()).len(), 1);
}

#[test]
fn insufficient_disk_waits_with_its_typed_reason_and_creates_nothing() {
    let temp = tempfile::tempdir().unwrap();
    let kernel = Kernel::new();
    // 48 GiB file plus 10% of a 4000 GiB filesystem does not fit in 400 GiB.
    kernel.state().disk_available = 400 * GIB + 47 * GIB;
    let helper = helper(temp.path(), &kernel);
    helper.reconcile_swap_at(None, false, 1000).unwrap();
    assert!(kernel.commands().is_empty());
    assert!(managed_files(temp.path()).is_empty());
    let record = checkpoint(temp.path());
    assert_eq!(record.state, SwapApplyState::Waiting);
    assert_eq!(record.reason, Some(SwapWaitReason::InsufficientDisk));
    assert!(
        record.detail.unwrap().contains(&(447 * GIB).to_string()),
        "names the measured space"
    );
    // Space freed: the next attempt applies it.
    kernel.state().disk_available = 800 * GIB;
    helper.reconcile_swap_at(None, false, 1060).unwrap();
    assert_eq!(kernel.total(), 64 * GIB);
}

#[test]
fn a_failed_swapon_leaves_no_file_and_retries_until_the_deadline_then_hourly() {
    let temp = tempfile::tempdir().unwrap();
    let kernel = Kernel::new();
    kernel.state().fail = Some(SWAPON);
    let helper = helper(temp.path(), &kernel);
    helper.reconcile_swap_at(None, false, 1000).unwrap();
    assert!(managed_files(temp.path()).is_empty());
    assert_eq!(
        kernel.total(),
        16 * GIB - header_bytes(),
        "OS swap untouched"
    );
    let record = checkpoint(temp.path());
    assert_eq!(record.reason, Some(SwapWaitReason::CommandFailed));
    assert!(record.detail.unwrap().contains("injected failure"));
    // Paced: an inventory before the next attempt runs nothing.
    let before = kernel.commands().len();
    helper.reconcile_swap_at(None, false, 1030).unwrap();
    assert_eq!(kernel.commands().len(), before);
    helper
        .reconcile_swap_at(None, false, 1000 + ATTEMPT_SECONDS)
        .unwrap();
    assert_eq!(checkpoint(temp.path()).state, SwapApplyState::Expired);
    // Expired stays quiet on inventory; the hourly pass opens a fresh window.
    kernel.state().fail = None;
    helper.reconcile_swap_at(None, false, 2000).unwrap();
    assert_eq!(kernel.total(), 16 * GIB - header_bytes());
    helper.reconcile_swap_at(None, true, 5000).unwrap();
    assert_eq!(kernel.total(), 64 * GIB);
    assert_eq!(checkpoint(temp.path()).state, SwapApplyState::Applied);
}

#[test]
fn a_reboot_or_crash_leftover_is_removed_and_the_file_recreated() {
    let temp = tempfile::tempdir().unwrap();
    let kernel = Kernel::new();
    let helper = helper(temp.path(), &kernel);
    helper.reconcile_swap_at(None, false, 1000).unwrap();
    let before = managed_files(temp.path());
    // Reboot: the kernel forgot the file; an interrupted creation left a temp.
    {
        let mut state = kernel.state();
        state.active.retain(|swap| swap.path == Path::new(OS_SWAP));
        state.boot = "boot-2".to_owned();
    }
    fs::write(
        temp.path().join(DIRECTORY).join(format!(
            "{TEMPORARY_PREFIX}{}{TEMPORARY_SUFFIX}",
            uuid::Uuid::new_v4()
        )),
        b"partial",
    )
    .unwrap();
    helper.reconcile_swap_at(None, false, 2000).unwrap();
    assert_eq!(kernel.total(), 64 * GIB);
    let after = managed_files(temp.path());
    assert_eq!(after.len(), 1);
    assert_ne!(after, before);
    assert_eq!(
        checkpoint(temp.path()).applied_boot_id.as_deref(),
        Some("boot-2")
    );
}

#[test]
fn an_older_generation_never_overrides_and_a_changed_default_in_the_same_one_re_plans() {
    let temp = tempfile::tempdir().unwrap();
    let kernel = Kernel::new();
    let helper = helper(temp.path(), &kernel);
    helper
        .reconcile_swap_at(Some(&intent(5, 96)), false, 1000)
        .unwrap();
    helper
        .reconcile_swap_at(Some(&intent(4, 32)), false, 1010)
        .unwrap();
    assert_eq!(kernel.total(), 96 * GIB);
    // A Controller release that changes the policy default keeps the
    // selection generation: the signed grant steers, never a refusal.
    helper
        .reconcile_swap_at(Some(&intent(5, 32)), false, 1020)
        .unwrap();
    assert_eq!(kernel.total(), 32 * GIB);
    assert_eq!(checkpoint(temp.path()).desired, intent(5, 32));
    assert!(matches!(
        helper.reconcile_swap_at(Some(&intent(6, 129)), false, 1030),
        Err(OperationError::InvalidOperation)
    ));
    assert_eq!(kernel.total(), 32 * GIB);
}

#[test]
fn a_helper_restart_that_detaches_its_mount_keeps_the_live_file_managed() {
    let temp = tempfile::tempdir().unwrap();
    let kernel = Kernel::new();
    let helper = helper(temp.path(), &kernel);
    helper
        .reconcile_swap_at(Some(&intent(1, 64)), false, 1000)
        .unwrap();
    let live = managed_files(temp.path());
    // A restarted helper has a new private mount namespace: the kernel now
    // prints the file through the detached mount of the old one.
    for swap in &mut kernel.state().active {
        if swap.path != Path::new(OS_SWAP) {
            swap.path = Path::new("/swap").join(swap.path.file_name().unwrap());
        }
    }
    helper.reconcile_swap_at(None, true, 5000).unwrap();
    assert_eq!(
        managed_files(temp.path()),
        live,
        "the live file is never unlinked"
    );
    assert_eq!(kernel.total(), 64 * GIB);
    assert_eq!(kernel.commands().len(), 3, "converged: nothing recreated");
    // And it can still be released by its own path.
    helper
        .reconcile_swap_at(Some(&intent(2, 16)), false, 5010)
        .unwrap();
    assert_eq!(kernel.total(), 16 * GIB - header_bytes());
    assert!(managed_files(temp.path()).is_empty());
}

#[test]
fn an_interrupted_swapoff_keeps_the_old_file_active_and_the_next_pass_releases_it() {
    let temp = tempfile::tempdir().unwrap();
    let kernel = Kernel::new();
    let helper = helper(temp.path(), &kernel);
    helper
        .reconcile_swap_at(Some(&intent(1, 96)), false, 1000)
        .unwrap();
    kernel.state().fail = Some(SWAPOFF);
    helper
        .reconcile_swap_at(Some(&intent(2, 64)), false, 1010)
        .unwrap();
    assert!(kernel.total() >= 96 * GIB, "never less swap than before");
    assert_eq!(managed_files(temp.path()).len(), 2);
    let record = checkpoint(temp.path());
    assert_eq!(record.state, SwapApplyState::Waiting);
    assert_eq!(record.reason, Some(SwapWaitReason::ReleaseDeferred));
    kernel.state().fail = None;
    helper.reconcile_swap_at(None, false, 1070).unwrap();
    assert_eq!(kernel.total(), 64 * GIB);
    assert_eq!(managed_files(temp.path()).len(), 1);
    assert_eq!(checkpoint(temp.path()).state, SwapApplyState::Applied);
}

#[test]
fn the_process_boundary_accepts_only_managed_swap_paths() {
    let directory = swap_directory();
    let managed = directory
        .join(format!("swap-{}.img", uuid::Uuid::new_v4()))
        .to_string_lossy()
        .into_owned();
    assert!(allowed_arguments(
        &directory,
        SWAPON,
        std::slice::from_ref(&managed)
    ));
    assert!(allowed_arguments(
        &directory,
        SWAPOFF,
        std::slice::from_ref(&managed)
    ));
    let elsewhere = format!("/tmp/x/swap/swap-{}.img", uuid::Uuid::new_v4());
    let root_swap = format!("/swap/swap-{}.img", uuid::Uuid::new_v4());
    for path in [
        "/swap.img".to_owned(),
        "/var/lib/vonk-forge/swap/../swap.img".to_owned(),
        "/var/lib/vonk-forge/swap/swap-not-a-uuid.img".to_owned(),
        "relative/swap/swap.img".to_owned(),
        elsewhere,
        root_swap,
    ] {
        assert!(
            !allowed_arguments(&directory, SWAPOFF, std::slice::from_ref(&path)),
            "{path}"
        );
    }
    assert!(!allowed_arguments(&directory, SWAPON, &["-a".to_owned()]));
    assert!(!allowed_arguments(
        &directory,
        FALLOCATE,
        &[
            "--length".to_owned(),
            (129 * GIB + 2 * header_bytes()).to_string(),
            managed.clone()
        ]
    ));
    assert!(allowed_arguments(
        &directory,
        FALLOCATE,
        &[
            "--length".to_owned(),
            (48 * GIB + header_bytes()).to_string(),
            managed
        ]
    ));
}

#[test]
fn a_crash_left_checkpoint_temporary_is_removed_on_the_next_pass() {
    let temp = tempfile::tempdir().unwrap();
    let kernel = Kernel::new();
    let helper = helper(temp.path(), &kernel);
    helper.reconcile_swap_at(None, false, 1000).unwrap();
    let leftover = temp.path().join(DIRECTORY).join(format!(
        "{TEMPORARY_PREFIX}{}{TEMPORARY_SUFFIX}",
        uuid::Uuid::new_v4()
    ));
    fs::write(&leftover, b"{").unwrap();
    helper.reconcile_swap_at(None, true, 5000).unwrap();
    assert!(!leftover.exists());
    assert_eq!(kernel.total(), 64 * GIB);
}

#[test]
fn proc_swaps_rows_parse_in_bytes() {
    let rows = parse_swaps(
        "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n/swap.img                               file\t\t16777212\t140248\t\t-2\n",
    )
    .unwrap();
    assert_eq!(
        rows,
        [ActiveSwap {
            path: PathBuf::from("/swap.img"),
            size_bytes: 16_777_212 * 1024,
            used_bytes: 140_248 * 1024,
        }]
    );
    assert!(parse_swaps("garbage").is_none());
}
