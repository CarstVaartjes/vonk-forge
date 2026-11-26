#![cfg(test)]
use super::{
    DIAGNOSTIC_LIMIT, DIAGNOSTIC_TRUNCATED, ProcessDiskReserve, ProcessRunner, Program,
    SystemProcessRunner, directory_bytes, podman_image_tmpdir, present_during_scan,
    subprocess_environment, transient_user_service_unit,
};
use std::{
    fs,
    io::{Read, Seek, SeekFrom, Write},
    net::TcpListener,
    os::unix::fs::symlink,
    thread,
    time::{Duration, Instant},
};
use tempfile::{tempdir, tempfile};

#[test]
fn failed_child_keeps_its_numeric_status_and_own_stderr() {
    // Curl rejects this option before attempting any network access.
    let output = SystemProcessRunner
        .run(
            Program::Curl,
            &["--vonk-unknown-option".into()],
            Duration::from_secs(1),
        )
        .unwrap();
    assert!(!output.success);
    assert_eq!(output.exit_code, Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("--vonk-unknown-option"));
}

#[test]
fn podman_and_its_user_service_wrapper_use_the_effective_users_systemd_bus() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("podman-storage");
    let arguments = vec!["--root".to_owned(), root.display().to_string()];
    for program in [Program::Podman, Program::SystemdRun] {
        let environment = subprocess_environment(program, 128, &arguments);

        assert_eq!(
            environment["XDG_CONFIG_HOME"],
            "/var/lib/vonk-forge-agent/.config"
        );
        assert_eq!(environment["XDG_RUNTIME_DIR"], "/run/user/128");
        assert_eq!(
            environment["DBUS_SESSION_BUS_ADDRESS"],
            "unix:path=/run/user/128/bus"
        );
        assert_eq!(
            environment["TMPDIR"],
            temporary
                .path()
                .join("podman-image-tmp")
                .display()
                .to_string()
        );
    }
    assert_eq!(
        podman_image_tmpdir(&arguments),
        Some(temporary.path().join("podman-image-tmp"))
    );
}

#[test]
fn non_podman_tools_keep_the_private_agent_runtime() {
    let environment = subprocess_environment(Program::Curl, 128, &[]);

    assert_eq!(environment["XDG_RUNTIME_DIR"], "/run/vonk-forge-agent");
    assert!(!environment.contains_key("DBUS_SESSION_BUS_ADDRESS"));
}

#[test]
fn only_exact_compiled_build_services_are_stoppable() {
    let arguments = vec![
        "--user".to_owned(),
        "--unit=vonk-recipe-build-00000000-0000-4000-8000-000000000002".to_owned(),
    ];
    assert_eq!(
        transient_user_service_unit(Program::SystemdRun, &arguments),
        Some("vonk-recipe-build-00000000-0000-4000-8000-000000000002")
    );
    assert_eq!(
        transient_user_service_unit(
            Program::SystemdRun,
            &["--unit=unrelated.service".to_owned()]
        ),
        None
    );
    assert_eq!(
        transient_user_service_unit(
            Program::SystemdRun,
            &[
                "--user".to_owned(),
                "--unit=vonk-recipe-build-../../other".to_owned(),
            ]
        ),
        None
    );
}

#[test]
fn storage_scan_ignores_paths_removed_by_the_running_process() {
    let directory = tempdir().unwrap();
    let removed = directory.path().join("transient-layer");

    assert!(
        present_during_scan(fs::metadata(removed))
            .unwrap()
            .is_none()
    );
}

#[test]
fn expired_or_cancelled_storage_observation_ends_and_fresh_scan_runs() {
    let directory = tempdir().unwrap();
    fs::write(directory.path().join("layer"), b"payload").unwrap();
    assert!(super::directory_bytes_until(directory.path(), Instant::now(), None).is_err());
    assert!(
        super::directory_bytes_until(
            directory.path(),
            Instant::now() + Duration::from_secs(1),
            Some(&|| true)
        )
        .is_err()
    );
    assert_eq!(directory_bytes(directory.path()).unwrap(), 7);
}

#[test]
fn storage_accounting_counts_a_symlink_without_following_it() {
    let directory = tempdir().unwrap();
    symlink("/usr/bin", directory.path().join("bin")).unwrap();

    assert_eq!(directory_bytes(directory.path()).unwrap(), 8);
}

#[test]
fn storage_accounting_counts_hardlinked_layer_payload_once() {
    let directory = tempdir().unwrap();
    let layer = directory.path().join("layer");
    fs::write(&layer, b"immutable-layer").unwrap();
    fs::hard_link(&layer, directory.path().join("reused-layer")).unwrap();

    assert_eq!(directory_bytes(directory.path()).unwrap(), 15);
}

#[test]
fn system_runner_truncates_verbose_diagnostics_without_killing_the_process() {
    let directory = tempdir().unwrap();
    let payload = directory.path().join("verbose-diagnostics");
    fs::write(&payload, [b'x'; 1024].repeat(1024)).unwrap();
    let output = SystemProcessRunner
        .run(
            Program::Curl,
            &[
                "--silent".to_owned(),
                format!("file://{}", payload.display()),
            ],
            Duration::from_secs(5),
        )
        .unwrap();
    assert!(output.success);
    assert_eq!(
        output.stdout.len(),
        usize::try_from(DIAGNOSTIC_LIMIT).unwrap()
    );
    assert!(output.stdout.starts_with(DIAGNOSTIC_TRUNCATED));
    assert!(output.stdout.ends_with(&[b'x'; 1024]));
}

#[test]
fn system_runner_honors_a_small_diagnostic_ring_without_failing_the_process() {
    let directory = tempdir().unwrap();
    let payload = directory.path().join("verbose-diagnostics");
    fs::write(&payload, [b'x'; 1024].repeat(1024)).unwrap();
    let output = SystemProcessRunner
        .run_bounded_directory_with_output_limit(
            Program::Curl,
            &[
                "--silent".to_owned(),
                format!("file://{}", payload.display()),
            ],
            Duration::from_secs(5),
            directory.path(),
            2 * 1024 * 1024,
            16,
        )
        .unwrap();
    assert!(output.success);
    assert_eq!(output.stdout.len(), 16);
    assert_eq!(output.stdout, DIAGNOSTIC_TRUNCATED[..16]);
}

#[test]
fn system_runner_rejects_work_that_would_violate_the_filesystem_reserve() {
    let directory = tempdir().unwrap();
    let result = SystemProcessRunner.run_with_disk_reserve_cancellable(
        Program::Curl,
        &["--version".to_owned()],
        Duration::from_secs(5),
        ProcessDiskReserve::new(directory.path(), u64::MAX),
        &|| false,
    );

    assert!(result.is_err());
    let fresh = SystemProcessRunner
        .run_with_disk_reserve_cancellable(
            Program::Curl,
            &["--version".to_owned()],
            Duration::from_secs(5),
            ProcessDiskReserve::new(directory.path(), 0),
            &|| false,
        )
        .unwrap();
    assert!(fresh.success);
}

#[test]
fn system_runner_streams_a_large_artifact_to_a_bounded_preopened_file() {
    let listener = TcpListener::bind(crate::client::test_support::FIXTURE_BIND).unwrap();
    let address = listener.local_addr().unwrap();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = [0_u8; 4096];
        let mut received = 0;
        while received < request.len()
            && !request[..received]
                .windows(4)
                .any(|window| window == b"\r\n\r\n")
        {
            let count = stream.read(&mut request[received..]).unwrap();
            assert!(count > 0);
            received += count;
        }
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\n")
            .unwrap();
        stream.write_all(&[b'x'; 1024 * 1024]).unwrap();
    });
    let mut sink = tempfile().unwrap();

    let output = SystemProcessRunner
        .run_to_file(
            Program::Curl,
            &["--silent".to_owned(), format!("http://{address}")],
            Duration::from_secs(5),
            &mut sink,
            1024 * 1024,
        )
        .unwrap();

    assert!(output.success);
    assert!(output.stdout.is_empty(), "{:?}", output.stdout);
    assert_eq!(sink.metadata().unwrap().len(), 1024 * 1024);
    sink.seek(SeekFrom::Start(0)).unwrap();
    let mut sample = [0_u8; 16];
    sink.read_exact(&mut sample).unwrap();
    assert_eq!(sample, [b'x'; 16]);
    server.join().unwrap();
}

#[test]
fn curl_enforces_the_download_body_limit() {
    let listener = TcpListener::bind(crate::client::test_support::FIXTURE_BIND).unwrap();
    let address = listener.local_addr().unwrap();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\n")
            .unwrap();
        #[expect(
            clippy::let_underscore_must_use,
            reason = "the peer may have closed the connection; the assertions are on the other side"
        )]
        let _ = stream.write_all(&[b'x'; 1024]);
    });
    let directory = tempdir().unwrap();
    let destination = directory.path().join("artifact");
    let output = SystemProcessRunner
        .run(
            Program::Curl,
            &[
                "--silent".to_owned(),
                "--show-error".to_owned(),
                "--max-filesize".to_owned(),
                "16".to_owned(),
                "--output".to_owned(),
                destination.display().to_string(),
                format!("http://{address}"),
            ],
            Duration::from_secs(5),
        )
        .unwrap();

    assert!(!output.success);
    if destination.exists() {
        assert!(fs::metadata(destination).unwrap().len() <= 16);
    }
    server.join().unwrap();
}
