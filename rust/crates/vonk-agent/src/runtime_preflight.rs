//! Offline rootless build probe, called from the claimed agent service operation.
//! Serving remains a separate signed-helper fact, completed by the executor.
use crate::{
    inventory::available_disk_bytes,
    process::{ProcessDiskReserve, ProcessError, ProcessRunner, Program},
    recipe_builder::{
        PodmanBuildStaging, podman_build_diagnostic, podman_storage_arguments_with_cgroup_manager,
    },
};
use std::{
    collections::BTreeMap,
    fs,
    os::unix::{ffi::OsStrExt, fs::MetadataExt},
    path::Path,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tempfile::Builder;
use vonk_agent_protocol::runtime_preflight::{
    RuntimePreflightFinding as Finding, RuntimePreflightRequest, RuntimePreflightResult,
    RuntimePreflightStatus as Status,
};
use vonk_agent_protocol::{canonical_json, hex_sha256};

pub const PROBE_BINARY: &str = "/usr/lib/vonk-forge/vonk-runtime-probe";

pub fn finding(capability: &str, passed: bool, failure: &str) -> Finding {
    Finding {
        capability: capability.into(),
        status: if passed {
            Status::Passed
        } else {
            Status::Failed
        },
        code: if passed {
            "available".into()
        } else {
            failure.into()
        },
    }
}

/// Includes effective systemd properties, package identities, kernel namespace
/// policy, runtime binaries and storage policy. Missing authority is an error,
/// never an empty fingerprint that could authorize a cached success.
pub fn host_fingerprint(
    runner: &impl ProcessRunner,
    agent_build_digest: &str,
    data_root: &Path,
    runtime_root: &Path,
) -> Result<String, ProcessError> {
    let mut values = BTreeMap::<String, String>::new();
    values.insert("agent_build".into(), agent_build_digest.into());
    values.insert("data_root".into(), data_root.display().to_string());
    values.insert("runtime_root".into(), runtime_root.display().to_string());
    for path in [
        "/proc/sys/kernel/osrelease",
        "/proc/sys/kernel/random/boot_id",
        "/etc/vonk-forge-agent/agent.toml",
        "/etc/subuid",
        "/etc/subgid",
        "/proc/sys/user/max_user_namespaces",
        "/etc/vonk-forge-agent/containers-storage.conf",
    ] {
        values.insert(path.into(), hex_sha256(&fs::read(path)?));
    }
    for path in [
        "/proc/sys/kernel/unprivileged_userns_clone",
        "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
        "/etc/containers/containers.conf",
        "/etc/docker/daemon.json",
    ] {
        match fs::read(path) {
            Ok(raw) => {
                values.insert(path.into(), hex_sha256(&raw));
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                values.insert(path.into(), "absent".into());
            }
            Err(error) => return Err(error.into()),
        }
    }
    for path in [
        "/usr/bin/podman",
        "/usr/bin/docker",
        "/usr/bin/dockerd",
        "/usr/bin/crun",
        "/usr/bin/fuse-overlayfs",
        PROBE_BINARY,
        "/usr/lib/vonk-forge/vonk-agent",
        "/usr/lib/vonk-forge/vonk-agent-helper",
    ] {
        let metadata = fs::metadata(path)?;
        values.insert(
            path.into(),
            format!(
                "{}:{}:{}:{}:{}",
                metadata.dev(),
                metadata.ino(),
                metadata.len(),
                metadata.mtime(),
                metadata.mtime_nsec()
            ),
        );
    }
    for unit in [
        "vonk-forge-agent.service",
        "vonk-forge-package-helper.service",
        "docker.service",
    ] {
        let output = runner.run(Program::Systemctl, &["show".into(), unit.into(), "--property=FragmentPath,DropInPaths,ExecStart,User,Group,Environment,PrivateTmp,PrivateDevices,ProtectSystem,ProtectHome,ProtectProc,ProcSubset,NoNewPrivileges,CapabilityBoundingSet,AmbientCapabilities,RestrictNamespaces,ReadWritePaths,ReadOnlyPaths,InaccessiblePaths,BindPaths,DeviceAllow,Delegate,RootDirectory,RootImage".into()], Duration::from_secs(2))?;
        if !output.success || output.stdout.is_empty() {
            return Err(std::io::Error::other("effective service policy unavailable").into());
        }
        values.insert(unit.into(), hex_sha256(&output.stdout));
    }
    Ok(hex_sha256(&canonical_json(&values).map_err(|_| {
        std::io::Error::other("fingerprint serialization")
    })?))
}

pub struct RuntimePreflight<'a, R> {
    pub runner: &'a R,
    pub data_root: &'a Path,
    pub runtime_root: &'a Path,
    pub probe_binary: &'a Path,
}

impl<R: ProcessRunner> RuntimePreflight<'_, R> {
    /// Caller has authenticated Controller connectivity by claiming this job.
    /// Fabric is observed inventory evidence, not a configuration advertisement.
    pub fn run(
        &self,
        request: &RuntimePreflightRequest,
        fingerprint: String,
        observed_fabric: Option<(&str, u64)>,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<RuntimePreflightResult, ProcessError> {
        request
            .validate()
            .map_err(|_| std::io::Error::other("preflight request invalid"))?;
        let started = Instant::now();
        let deadline = started + Duration::from_secs(40);
        let mut findings = vec![
            finding(
                "architecture",
                request.architecture
                    == if cfg!(target_arch = "aarch64") {
                        "linux-arm64"
                    } else {
                        "linux-amd64"
                    },
                "architecture_mismatch",
            ),
            finding("controller_reachable", true, "controller_unreachable"),
        ];
        for (capability, path) in [
            ("cache_writable", self.data_root.join("distribution")),
            ("staging_writable", self.data_root.join("build-staging")),
            ("temporary_directory", self.data_root.join("tmp")),
        ] {
            let writable = fs::create_dir_all(&path)
                .and_then(|()| tempfile::tempfile_in(&path).map(|_| ()))
                .is_ok();
            findings.push(finding(capability, writable, "directory_not_writable"));
        }
        let disk_ok = available_disk_bytes(self.data_root)
            .is_ok_and(|free| free >= request.minimum_free_bytes);
        findings.push(finding(
            "disk_reserve",
            disk_ok,
            "disk_reserve_insufficient",
        ));
        let fabric_ok = observed_fabric.is_some_and(|(kind, speed)| {
            speed >= request.fabric_minimum_mbps
                && match request.fabric_connectivity.as_str() {
                    "connected" => matches!(kind, "connected" | "full_mesh" | "switch"),
                    "full_mesh" => matches!(kind, "full_mesh" | "switch"),
                    "switch" => kind == "switch",
                    "none" => true,
                    _ => false,
                }
        });
        if request.fabric_connectivity != "none" {
            findings.push(Finding {
                capability: "fabric".into(),
                status: if observed_fabric.is_none() {
                    Status::Unknown
                } else if fabric_ok {
                    Status::Passed
                } else {
                    Status::Failed
                },
                code: if fabric_ok {
                    "available"
                } else {
                    "fabric_requirement_unverified"
                }
                .into(),
            });
        }
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| std::io::Error::other("clock invalid"))?
            .as_secs();
        let cache_path = self.data_root.join("runtime-preflight-cache.json");
        let previous = match fs::symlink_metadata(&cache_path) {
            Ok(metadata) => {
                if !metadata.is_file()
                    || metadata.file_type().is_symlink()
                    || metadata.uid() != rustix::process::geteuid().as_raw()
                    || metadata.mode() & 0o077 != 0
                    || metadata.len() > 65536
                {
                    return Err(std::io::Error::other("unsafe preflight cache").into());
                }
                let result: RuntimePreflightResult =
                    vonk_agent_protocol::parse_strict(&fs::read(&cache_path)?)
                        .map_err(|_| std::io::Error::other("invalid preflight cache"))?;
                result
                    .validate()
                    .map_err(|_| std::io::Error::other("invalid preflight cache"))?;
                Some(result)
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => return Err(error.into()),
        };
        let reused = previous.as_ref().map_or_else(Vec::new, |result| {
            reusable_build_findings(result, &fingerprint, request, now)
        });
        let cached = request.source_build && disk_ok && !reused.is_empty();
        if cached {
            findings.extend(reused);
        }
        if request.source_build
            && disk_ok
            && !cached
            && findings.iter().all(|item| item.status != Status::Failed)
        {
            self.build_probe(
                request.minimum_free_bytes,
                deadline,
                cancelled,
                &mut findings,
            )?;
        }
        findings.push(Finding {
            capability: "signed_helper_run".into(),
            status: Status::Unknown,
            code: "signed_helper_probe_required".into(),
        });
        for capability in &request.mandatory_capabilities {
            if !findings.iter().any(|value| &value.capability == capability) {
                findings.push(Finding {
                    capability: capability.clone(),
                    status: Status::Unknown,
                    code: "mandatory_capability_unknown".into(),
                });
            }
        }
        let result = RuntimePreflightResult {
            schema_version: 1,
            fingerprint,
            request_sha256: request
                .digest()
                .map_err(|_| std::io::Error::other("request digest invalid"))?,
            observed_at: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(|_| std::io::Error::other("clock invalid"))?
                .as_secs(),
            duration_ms: started.elapsed().as_millis() as u64,
            cached,
            findings,
        };
        // Never extend the observed age when reusing a successful build result.
        let mut cache_result = result.clone();
        if cached {
            cache_result.observed_at = previous.as_ref().unwrap().observed_at;
        }
        let temporary = tempfile::NamedTempFile::new_in(self.data_root)?;
        fs::write(
            temporary.path(),
            canonical_json(&cache_result)
                .map_err(|_| std::io::Error::other("cache serialization invalid"))?,
        )?;
        temporary.as_file().sync_all()?;
        temporary
            .persist(&cache_path)
            .map_err(|error| error.error)?;
        Ok(result)
    }

    fn build_probe(
        &self,
        minimum_free_bytes: u64,
        deadline: Instant,
        cancelled: &dyn Fn() -> bool,
        findings: &mut Vec<Finding>,
    ) -> Result<(), ProcessError> {
        let staging = PodmanBuildStaging::create(&self.data_root.join("build-staging"))?;
        fs::create_dir_all(self.runtime_root)?;
        let runroot = Builder::new().prefix("b-").tempdir_in(self.runtime_root)?;
        let path_ok = runroot.path().as_os_str().as_bytes().len() <= 50;
        findings.push(finding(
            "runroot_length",
            path_ok,
            "runroot_exceeds_50_bytes",
        ));
        if !path_ok {
            return Ok(());
        }
        let storage = staging.path().join("podman-storage");
        let tmp = staging.path().join("podman-image-tmp");
        let xdg = runroot.path().join("xdg");
        for path in [&storage, &tmp, &xdg] {
            fs::create_dir(path)?;
        }
        let context = staging.path().join("context");
        fs::create_dir(&context)?;
        let mut archive = tar::Builder::new(fs::File::create(context.join("rootfs.tar"))?);
        let mut executable = fs::File::open(self.probe_binary)?;
        let mut header = tar::Header::new_gnu();
        header.set_size(executable.metadata()?.len());
        header.set_uid(0);
        header.set_gid(0);
        header.set_mode(0o555);
        header.set_cksum();
        archive.append_data(&mut header, "probe", &mut executable)?;
        let mut header = tar::Header::new_gnu();
        header.set_entry_type(tar::EntryType::Directory);
        header.set_size(0);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mode(0o1777);
        header.set_cksum();
        archive.append_data(&mut header, "tmp", std::io::empty())?;
        archive.finish()?;
        drop(archive);
        fs::write(context.join("Containerfile"), b"FROM scratch\nADD rootfs.tar /\nRUN [\"/probe\"]\nUSER 65534:65534\nENTRYPOINT [\"/probe\"]\n")?;
        let tag = format!("localhost/vonk-preflight:{}", uuid::Uuid::new_v4());
        for (capability, command) in [
            (
                "podman_build",
                vec![
                    "build".into(),
                    "--no-cache".into(),
                    "--pull=never".into(),
                    "--cap-drop=all".into(),
                    "--security-opt=no-new-privileges".into(),
                    "--network=none".into(),
                    "--tag".into(),
                    tag.clone(),
                    context.display().to_string(),
                ],
            ),
            (
                "podman_run",
                vec![
                    "run".into(),
                    "--rm".into(),
                    "--pull=never".into(),
                    "--network=none".into(),
                    "--cap-drop=all".into(),
                    "--security-opt=no-new-privileges".into(),
                    "--read-only".into(),
                    "--tmpfs=/tmp:rw,nosuid,nodev,mode=1777,size=1m".into(),
                    "--user=65534:65534".into(),
                    tag.clone(),
                ],
            ),
        ] {
            let mut arguments = user_service_arguments(&xdg, &tmp);
            arguments.extend(podman_storage_arguments_with_cgroup_manager(
                &storage,
                runroot.path(),
                "cgroupfs",
            ));
            arguments.push("--runtime=/usr/bin/crun".into());
            arguments.extend(command);
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                findings.push(finding(capability, false, "deadline_exceeded"));
                break;
            }
            let outcome = self.runner.run_with_disk_reserve_cancellable(
                Program::SystemdRun,
                &arguments,
                remaining,
                ProcessDiskReserve::new(staging.path(), minimum_free_bytes),
                cancelled,
            );
            match outcome {
                Ok(output) => {
                    let passed = output.success;
                    findings.push(finding(capability, passed, &probe_diagnostic(&output)));
                    if !passed {
                        break;
                    }
                }
                Err(ProcessError::Cancelled) => return Err(ProcessError::Cancelled),
                Err(error) => {
                    findings.push(finding(
                        capability,
                        false,
                        match error {
                            ProcessError::Timeout => "deadline_exceeded",
                            ProcessError::StorageLimit => "disk_reserve_insufficient",
                            ProcessError::OutputLimit => "diagnostic_limit_exceeded",
                            _ => "subprocess_unavailable",
                        },
                    ));
                    break;
                }
            }
        }
        // These are isolated disposable graph/run roots, never the model or image cache.
        Ok(())
    }
}

fn probe_diagnostic(output: &crate::process::ProcessOutput) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr).to_ascii_lowercase();
    if stderr.contains("failed to connect to bus")
        || stderr.contains("failed to start transient")
        || stderr.contains("no medium found")
    {
        return "user-service-manager-unavailable".into();
    }
    if stderr.contains("default oci runtime") && stderr.contains("not found") {
        return "oci-runtime-unavailable".into();
    }
    let diagnostic = String::from_utf8_lossy(&output.stdout);
    for (code, finding) in [
        (21, "proc-unavailable"),
        (22, "capabilities-not-zero"),
        (23, "no-new-privileges-unavailable"),
        (24, "mount-namespace-unavailable"),
        (25, "temporary-directory-unavailable"),
    ] {
        if diagnostic.contains(&format!("vonk-runtime-preflight-error:{code}")) {
            return finding.into();
        }
    }
    podman_build_diagnostic(output).to_string()
}

fn user_service_arguments(xdg: &Path, tmp: &Path) -> Vec<String> {
    vec![
        "--user".into(),
        "--wait".into(),
        "--pipe".into(),
        "--collect".into(),
        "--quiet".into(),
        "--service-type=exec".into(),
        format!(
            "--unit=vonk-recipe-build-preflight-{}",
            uuid::Uuid::new_v4()
        ),
        "--setenv=HOME=/var/lib/vonk-forge-agent".into(),
        "--setenv=XDG_CONFIG_HOME=/var/lib/vonk-forge-agent/.config".into(),
        "--setenv=XDG_DATA_HOME=/var/lib/vonk-forge-agent".into(),
        format!("--setenv=XDG_RUNTIME_DIR={}", xdg.display()),
        format!("--setenv=TMPDIR={}", tmp.display()),
        "--setenv=CONTAINERS_STORAGE_CONF=/etc/vonk-forge-agent/containers-storage.conf".into(),
        "--property=MemoryMax=268435456".into(),
        "--property=CPUQuota=100%".into(),
        "--property=TasksMax=128".into(),
        "--property=RuntimeMaxSec=40s".into(),
        "--property=TimeoutStopSec=2s".into(),
        "--property=KillMode=control-group".into(),
        "/usr/bin/podman".into(),
    ]
}

/// Cache identity includes the request and host fingerprint; dynamic resources
/// (disk, reachability, fabric) must still be observed on every new claim.
pub fn reusable_build_findings(
    result: &RuntimePreflightResult,
    fingerprint: &str,
    request: &RuntimePreflightRequest,
    now: u64,
) -> Vec<Finding> {
    if result.validate().is_err()
        || result.fingerprint != fingerprint
        || request.digest().ok().as_deref() != Some(result.request_sha256.as_str())
        || now < result.observed_at
        || now - result.observed_at > 300
    {
        return vec![];
    }
    let findings: Vec<_> = result
        .findings
        .iter()
        .filter(|value| {
            matches!(
                value.capability.as_str(),
                "podman_build" | "podman_run" | "runroot_length"
            )
        })
        .cloned()
        .collect();
    if findings.len() == 3 && findings.iter().all(|value| value.status == Status::Passed) {
        findings
    } else {
        vec![]
    }
}

#[cfg(test)]
mod tests {
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
            self.calls.borrow_mut().push(arguments.to_vec());
            Ok(ProcessOutput {
                success: self.fail.is_none(),
                stdout: b"vonk-runtime-preflight-ok\n".to_vec(),
                stderr: self.fail.unwrap_or("").as_bytes().to_vec(),
            })
        }
    }
    fn request() -> RuntimePreflightRequest {
        RuntimePreflightRequest {
            schema_version: 1,
            architecture: if cfg!(target_arch = "aarch64") {
                "linux-arm64"
            } else {
                "linux-amd64"
            }
            .into(),
            source_build: true,
            minimum_free_bytes: 0,
            fabric_connectivity: "none".into(),
            fabric_minimum_mbps: 0,
            mandatory_capabilities: vec![],
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
                    .any(|arg| arg.starts_with("--setenv=TMPDIR=")
                        && arg.contains("podman-image-tmp"))
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
    fn changed_host_or_request_reexecutes_cached_probe_and_refreshes_dynamic_facts() {
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
        assert!(!first.cached);
        assert!(second.cached);
        assert_eq!(runner.calls.borrow().len(), 2);
        assert!(
            reusable_build_findings(&first, &"b".repeat(64), &req, first.observed_at).is_empty()
        );
        assert!(
            reusable_build_findings(&first, &"a".repeat(64), &req, first.observed_at + 301)
                .is_empty()
        );
        assert!(
            !preflight
                .run(&req, "b".repeat(64), None, &|| false)
                .unwrap()
                .cached
        );
        assert_eq!(runner.calls.borrow().len(), 4);
        let mut changed = req;
        changed.minimum_free_bytes = u64::MAX;
        let disk = preflight
            .run(&changed, "b".repeat(64), None, &|| false)
            .unwrap();
        assert_eq!(status(&disk, "disk_reserve").status, Status::Failed);
        assert_eq!(runner.calls.borrow().len(), 4);
    }

    #[test]
    fn proc_namespace_and_temporary_storage_failures_have_concrete_findings() {
        for (diagnostic, code) in [
            ("crun: mount `proc` permission denied", "proc-mount-denied"),
            (
                "cannot clone: Operation not permitted",
                "user-namespace-denied",
            ),
            ("no space left on device", "temporary-storage-exhausted"),
            (
                "fuse-overlayfs: storage driver failed",
                "storage-driver-failure",
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
            assert_eq!(status(&result, "podman_build").code, code);
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
        req.fabric_connectivity = "full_mesh".into();
        req.fabric_minimum_mbps = 100000;
        let result = RuntimePreflight {
            runner: &runner,
            data_root: data.path(),
            runtime_root: &runtime,
            probe_binary: Path::new("/unneeded"),
        }
        .run(&req, "a".repeat(64), None, &|| false)
        .unwrap();
        assert_eq!(
            status(&result, "runroot_length").code,
            "runroot_exceeds_50_bytes"
        );
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
        assert_eq!(status(&result, "fabric").status, Status::Failed);
    }
}
