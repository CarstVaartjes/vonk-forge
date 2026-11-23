#![cfg(test)]
use super::*;
use crate::process::ProcessOutput;
use std::cell::RefCell;

#[derive(Default)]
struct Runner {
    calls: RefCell<Vec<Vec<String>>>,
    fail: Option<&'static str>,
}
impl ProcessRunner for Runner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        assert_eq!(program, Program::SystemdRun);
        assert!(timeout <= Duration::from_secs(40));
        if arguments.iter().any(|arg| arg == "build") {
            let context = arguments.last().unwrap();
            let mut archive =
                tar::Archive::new(fs::File::open(Path::new(context).join("rootfs.tar")).unwrap());
            let paths = archive
                .entries()
                .unwrap()
                .map(|entry| entry.unwrap().path().unwrap().into_owned())
                .collect::<Vec<_>>();
            assert!(paths.contains(&std::path::PathBuf::from("etc/passwd")));
            assert!(paths.contains(&std::path::PathBuf::from("etc/group")));
        }
        self.calls.borrow_mut().push(arguments.to_vec());
        Ok(ProcessOutput {
            exit_code: None,
            success: self.fail.is_none(),
            stdout: b"vonk-runtime-preflight-ok\n".to_vec(),
            stderr: self.fail.unwrap_or("").as_bytes().to_vec(),
        })
    }
}
fn request() -> RuntimePreflightRequest {
    RuntimePreflightRequest {
        source_build: true,
        minimum_free_bytes: 0,
        fabric_connectivity: "none".parse().unwrap(),
        fabric_minimum_mbps: 0,
    }
}
fn status<'a>(result: &'a RuntimePreflightResult, capability: &str) -> &'a Finding {
    result
        .findings
        .iter()
        .find(|value| value.capability == capability)
        .unwrap()
}

#[test]
fn offline_build_and_run_use_current_recipe_service_policy_and_zero_added_caps() {
    let data = tempfile::tempdir().unwrap();
    let runtime = tempfile::tempdir().unwrap();
    let probe = data.path().join("probe");
    fs::write(&probe, b"probe").unwrap();
    let runner = Runner::default();
    let preflight = RuntimePreflight {
        runner: &runner,
        data_root: data.path(),
        runtime_root: runtime.path(),
        probe_binary: &probe,
    };
    let result = preflight
        .run(&request(), "a".repeat(64), None, &|| false)
        .unwrap();
    assert_eq!(status(&result, "podman_build").status, Status::Passed);
    assert_eq!(status(&result, "podman_run").status, Status::Passed);
    assert_eq!(status(&result, "signed_helper_run").status, Status::Unknown);
    assert_eq!(runner.calls.borrow().len(), 2);
    for args in runner.calls.borrow().iter() {
        for flag in [
            "--user",
            "--service-type=exec",
            "--cgroup-manager=cgroupfs",
            "--runtime=/usr/bin/crun",
            "--pull=never",
            "--network=none",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
        ] {
            assert!(args.contains(&flag.into()), "missing {flag}");
        }
        assert!(
            args.iter()
                .any(|arg| arg.starts_with("--setenv=TMPDIR=") && arg.contains("podman-image-tmp"))
        );
        assert!(!args.iter().any(|arg| arg == "--privileged"
            || arg.starts_with("--cap-add")
            || arg == "--isolation=chroot"));
    }
    assert_eq!(
        fs::read_dir(data.path().join("build-staging"))
            .unwrap()
            .count(),
        0
    );
    assert_eq!(fs::read_dir(runtime.path()).unwrap().count(), 0);
}

#[test]
fn changed_host_reexecutes_cached_probe_and_refreshes_dynamic_facts() {
    let data = tempfile::tempdir().unwrap();
    let runtime = tempfile::tempdir().unwrap();
    let probe = data.path().join("probe");
    fs::write(&probe, b"probe").unwrap();
    let runner = Runner::default();
    let preflight = RuntimePreflight {
        runner: &runner,
        data_root: data.path(),
        runtime_root: runtime.path(),
        probe_binary: &probe,
    };
    let req = request();
    let first = preflight
        .run(&req, "a".repeat(64), None, &|| false)
        .unwrap();
    let second = preflight
        .run(&req, "a".repeat(64), None, &|| false)
        .unwrap();
    // The second run reuses the cached build probe.
    assert_eq!(status(&second, "podman_build").status, Status::Passed);
    assert_eq!(runner.calls.borrow().len(), 2);
    assert!(reusable_build_findings(&first, &"b".repeat(64), first.observed_at).is_empty());
    assert!(reusable_build_findings(&first, &"a".repeat(64), first.observed_at + 301).is_empty());
    preflight
        .run(&req, "b".repeat(64), None, &|| false)
        .unwrap();
    assert_eq!(runner.calls.borrow().len(), 4);
    let mut changed = req;
    changed.minimum_free_bytes = u64::MAX;
    let disk = preflight
        .run(&changed, "b".repeat(64), None, &|| false)
        .unwrap();
    assert_eq!(status(&disk, "disk_reserve").status, Status::Unknown);
    assert_eq!(runner.calls.borrow().len(), 4);
    changed.minimum_free_bytes = 0;
    let fresh = preflight
        .run(&changed, "c".repeat(64), None, &|| false)
        .unwrap();
    assert_eq!(status(&fresh, "podman_build").status, Status::Passed);
    assert_eq!(runner.calls.borrow().len(), 6);
}

#[test]
fn proc_namespace_and_temporary_storage_failures_have_concrete_findings() {
    for (diagnostic, _code) in [
        (
            "crun: mount `proc` permission denied",
            Code::PreflightFindingProcMountDenied,
        ),
        (
            "cannot clone: Operation not permitted",
            Code::PreflightFindingUserNamespaceDenied,
        ),
        (
            "no space left on device",
            Code::PreflightFindingTemporaryStorageExhausted,
        ),
        (
            "fuse-overlayfs: storage driver failed",
            Code::PreflightFindingStorageDriverFailure,
        ),
    ] {
        let data = tempfile::tempdir().unwrap();
        let runtime = tempfile::tempdir().unwrap();
        let probe = data.path().join("probe");
        fs::write(&probe, b"probe").unwrap();
        let runner = Runner {
            fail: Some(diagnostic),
            ..Default::default()
        };
        let result = RuntimePreflight {
            runner: &runner,
            data_root: data.path(),
            runtime_root: runtime.path(),
            probe_binary: &probe,
        }
        .run(&request(), "a".repeat(64), None, &|| false)
        .unwrap();
        assert!(
            !reusable_build_findings(&result, &"a".repeat(64), result.observed_at)
                .iter()
                .any(|value| value.capability == "podman_build")
        );
        let repaired = Runner::default();
        let fresh = RuntimePreflight {
            runner: &repaired,
            data_root: data.path(),
            runtime_root: runtime.path(),
            probe_binary: &probe,
        }
        .run(&request(), "a".repeat(64), None, &|| false)
        .unwrap();
        assert_eq!(status(&fresh, "podman_build").status, Status::Passed);
        assert_eq!(runner.calls.borrow().len(), 1);
    }
}

#[test]
fn excessive_runroot_and_unverified_fabric_do_not_become_success() {
    let data = tempfile::tempdir().unwrap();
    let runtime = data
        .path()
        .join("runtime-root-with-a-deliberately-long-path-for-podman");
    let runner = Runner::default();
    let mut req = request();
    req.fabric_connectivity = "connected".parse().unwrap();
    req.fabric_minimum_mbps = 300000;
    let result = RuntimePreflight {
        runner: &runner,
        data_root: data.path(),
        runtime_root: &runtime,
        probe_binary: Path::new("/unneeded"),
    }
    .run(&req, "a".repeat(64), None, &|| false)
    .unwrap();
    assert!(reusable_build_findings(&result, &"a".repeat(64), result.observed_at).is_empty());
    assert_eq!(status(&result, "fabric").status, Status::Unknown);
    assert!(runner.calls.borrow().is_empty());
    req.source_build = false;
    let result = RuntimePreflight {
        runner: &runner,
        data_root: data.path(),
        runtime_root: &runtime,
        probe_binary: Path::new("/unneeded"),
    }
    .run(&req, "a".repeat(64), Some(("connected", 200000)), &|| false)
    .unwrap();
    assert_eq!(status(&result, "fabric").status, Status::Unknown);
    let short_runtime = tempfile::tempdir().unwrap();
    let binary = data.path().join("probe");
    fs::write(&binary, b"probe").unwrap();
    req.source_build = true;
    let fresh = RuntimePreflight {
        runner: &runner,
        data_root: data.path(),
        runtime_root: short_runtime.path(),
        probe_binary: &binary,
    }
    .run(&req, "b".repeat(64), Some(("connected", 400000)), &|| false)
    .unwrap();
    assert_eq!(status(&fresh, "fabric").status, Status::Passed);
    assert_eq!(status(&fresh, "podman_build").status, Status::Passed);
}
