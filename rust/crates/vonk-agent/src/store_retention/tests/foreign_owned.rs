//! Root-only boundary fault: an unprivileged agent and a real helper executor.
use super::*;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::net::UnixListener;
use std::os::unix::process::CommandExt;
use std::process::Command;
use vonk_agent_helper::operations::{
    CommandOutput, CommandRunner, ManagedRoots, OperationExecutor,
};
use vonk_agent_protocol::generated::{HostHelperResponse, HostHelperResponseStatus};

struct EmptyDocker;
impl CommandRunner for EmptyDocker {
    fn run(&self, _: &Path, args: &[String]) -> Result<CommandOutput, String> {
        assert!(args.starts_with(&["container".into(), "ls".into()]));
        Ok(CommandOutput {
            success: true,
            stdout: vec![],
            stderr: vec![],
            exit_code: Some(0),
        })
    }
}

#[test]
#[ignore = "requires root; run in the disposable Linux fault-injection container"]
fn foreign_owned_scopes_leave_through_helper_with_progress() {
    // Catches recursive agent chmod/unlink of workload/helper-owned private
    // directories: the production EPERM must converge, without changing owners.
    assert!(rustix::process::geteuid().is_root());
    let root = tempfile::tempdir().unwrap();
    let agent = root.path().join("agent");
    let roots = ManagedRoots::under(&root.path().join("helper")).with_agent_data(&agent);
    fs::create_dir_all(&agent).unwrap();
    for path in [root.path(), &agent] {
        rustix::fs::chown(
            path,
            Some(rustix::process::Uid::from_raw(10000)),
            Some(rustix::process::Gid::from_raw(10000)),
        )
        .unwrap();
    }
    for store in ["runs", "installations"] {
        let parent = agent.join(store);
        let scope = parent.join(INSTALLATION);
        fs::create_dir_all(&scope).unwrap();
        for path in [&parent, &scope] {
            fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
            rustix::fs::chown(
                path,
                Some(rustix::process::Uid::from_raw(10000)),
                Some(rustix::process::Gid::from_raw(10000)),
            )
            .unwrap();
        }
        // Helper-created cache / workload-created tmp, private to another uid.
        let private = scope.join("private");
        fs::create_dir(&private).unwrap();
        fs::set_permissions(&private, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(private.join("root-owned"), b"cannot unlink as agent").unwrap();
    }
    let socket = root.path().join("helper.sock");
    let listener = UnixListener::bind(&socket).unwrap();
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o666)).unwrap();
    listener.set_nonblocking(true).unwrap();
    let executor = OperationExecutor::new(roots, &[0; 32], EmptyDocker, None)
        .unwrap()
        .with_runtime_request_owner(10000);
    let server = std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(20);
        let mut removed = 0;
        while removed < 2 && Instant::now() < deadline {
            let (mut stream, _) = match listener.accept() {
                Ok(peer) => peer,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(10));
                    continue;
                }
                Err(error) => panic!("{error}"),
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let raw = vonk_agent_helper::protocol::read_frame(&mut stream).unwrap();
            let request = vonk_agent_protocol::parse_strict(&raw).unwrap();
            executor.remove_agent_store(&request).unwrap();
            let response = HostHelperResponse {
                schema_version: 1,
                request_id: Some(request.request_id),
                status: HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
                exit_code: Some(0),
                error_code: None,
                diagnostic: None,
                process_running: None,
                process_logs: None,
                installation_intent_nonce: None,
                job_progress_at: None,
            };
            vonk_agent_helper::protocol::write_frame(
                &mut stream,
                &vonk_agent_protocol::canonical_generated_json(&response).unwrap(),
            )
            .unwrap();
            removed += 1;
        }
        removed
    });
    let output = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "store_retention::tests::foreign_owned::unprivileged_retention_child",
            "--nocapture",
        ])
        .env("VONK_RETPERM_FAULT_ROOT", root.path())
        .uid(10000)
        .gid(10000)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(server.join().unwrap(), 2);
    for store in ["runs", "installations"] {
        assert!(!agent.join(store).join(INSTALLATION).exists());
    }
}

#[test]
fn unprivileged_retention_child() {
    let Some(root) = std::env::var_os("VONK_RETPERM_FAULT_ROOT") else {
        return;
    };
    let root = PathBuf::from(root);
    let agent = root.join("agent");
    assert_eq!(rustix::process::geteuid().as_raw(), 10000);
    for store in ["runs", "installations"] {
        let scope = agent.join(store).join(INSTALLATION);
        assert_ne!(fs::metadata(scope.join("private")).unwrap().uid(), 10000);
        assert_eq!(
            fs::remove_file(scope.join("private/root-owned"))
                .unwrap_err()
                .kind(),
            std::io::ErrorKind::PermissionDenied
        );
        assert_eq!(
            crate::recipe_builder::make_owned_directories_removable(&scope)
                .unwrap_err()
                .kind(),
            std::io::ErrorKind::PermissionDenied
        );
    }
    let verdict = verdict(&[INSTALLATION], Some(&[INSTALLATION]));
    let remove = |request: &_| {
        crate::host_runtime::remove_agent_store_at(&root.join("helper.sock"), request)
    };
    let run = |now| {
        super::super::sweep_with_remover(
            &agent,
            now,
            Instant::now() + Duration::from_secs(10),
            Some(&verdict),
            &|| ROOMY,
            &|| {},
            &remove,
        )
    };
    assert_eq!(run(SystemTime::now()).entries, 0);
    let progress = run(after_grace());
    assert_eq!(progress.entries, 2);
    assert!(!progress.pending);
    assert!(super::super::retention_failure(&agent).is_none());
}
