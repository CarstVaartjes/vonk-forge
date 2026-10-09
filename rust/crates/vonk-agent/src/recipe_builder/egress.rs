//! Egress.

use super::*;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum BuildNetwork {
    None,
    Public,
}

/// An empty host list builds offline; otherwise the build reaches only the
/// listed public hosts.
pub(super) fn build_network(network: &vonk_agent_protocol::RecipeBuildNetwork) -> BuildNetwork {
    if network.hosts.is_empty() {
        BuildNetwork::None
    } else {
        BuildNetwork::Public
    }
}

pub(super) struct BuildEgress<'a, R: ProcessRunner + ?Sized> {
    pub(super) runner: &'a R,
    pub(super) storage: &'a Path,
    pub(super) runroot: &'a Path,
    pub(super) staging: &'a Path,
    pub(super) internal_network: String,
    pub(super) outbound_network: String,
    pub(super) proxy_name: String,
    pub(super) proxy_unit: String,
    pub(super) image: String,
}

pub(super) struct BuildEgressStart<'a> {
    pub(super) storage: &'a Path,
    pub(super) runroot: &'a Path,
    pub(super) staging: &'a Path,
    pub(super) binary: &'a Path,
    pub(super) operation_id: Uuid,
    pub(super) hosts: &'a [String],
    pub(super) minimum_free_disk_bytes: u64,
    pub(super) deadline: Instant,
    pub(super) cancelled: &'a dyn Fn() -> bool,
}

impl<'a, R: ProcessRunner + ?Sized> BuildEgress<'a, R> {
    pub(super) fn start(
        runner: &'a R,
        context: BuildEgressStart<'a>,
    ) -> Result<Self, RecipeBuildError> {
        let suffix = context.operation_id.simple().to_string();
        let internal_network = format!("vonk-build-in-{suffix}");
        let outbound_network = format!("vonk-build-out-{suffix}");
        let proxy_name = format!("vonk-build-proxy-{suffix}");
        let image = format!("localhost/vonk/build-egress:{suffix}");
        let rootfs = context.staging.join("build-egress-rootfs.tar");
        write_proxy_rootfs(context.binary, &rootfs)?;
        let result = Self {
            runner,
            storage: context.storage,
            runroot: context.runroot,
            staging: context.staging,
            internal_network,
            outbound_network,
            proxy_name,
            proxy_unit: format!("vonk-recipe-build-{}-e", context.operation_id),
            image,
        };
        let imported = result.run_with_file(
            &[
                "import",
                "--quiet",
                "--change",
                "ENTRYPOINT [\"/vonk-build-egress\"]",
                "-",
                &result.image,
            ],
            &rootfs,
            context.staging,
            context.minimum_free_disk_bytes,
            phase_time(context.deadline, Duration::from_secs(120))?,
            context.cancelled,
        )?;
        if !imported.success {
            return Err(network_boundary_error(
                FailureStage::EgressImageImport,
                &imported,
            ));
        }
        // Neither network needs container-name discovery. Keeping Aardvark
        // out of these private bridges also avoids competing DNS lifecycles
        // between rootless Podman and Buildah's OCI network setup. The proxy
        // resolves public hosts through the ordinary outbound nameservers.
        for arguments in [
            vec![
                "network".to_owned(),
                "create".to_owned(),
                "--internal".to_owned(),
                "--disable-dns".to_owned(),
                result.internal_network.clone(),
            ],
            vec![
                "network".to_owned(),
                "create".to_owned(),
                "--disable-dns".to_owned(),
                result.outbound_network.clone(),
            ],
        ] {
            let output = result.run_cancellable(
                &arguments,
                phase_time(context.deadline, Duration::from_secs(30))?,
                context.cancelled,
            )?;
            if !output.success {
                return Err(network_boundary_error(
                    FailureStage::EgressNetworkCreate,
                    &output,
                ));
            }
        }
        let mut arguments = vec![
            "run".to_owned(),
            "--rm".to_owned(),
            "--name".to_owned(),
            result.proxy_name.clone(),
            "--network".to_owned(),
            format!("{},{}", result.outbound_network, result.internal_network),
            "--read-only".to_owned(),
            "--cap-drop=all".to_owned(),
            "--security-opt=no-new-privileges".to_owned(),
            "--pids-limit=96".to_owned(),
            "--memory=134217728b".to_owned(),
            "--cpus=1".to_owned(),
            "--user=65532:65532".to_owned(),
            result.image.clone(),
        ];
        for host in context.hosts {
            arguments.push("--allow-host".to_owned());
            arguments.push(host.clone());
        }
        // Keep foreground Podman and conmon owned by a service for the whole
        // build. A detached container's launcher exits immediately; systemd
        // then kills conmon and leaves an unusable boundary behind.
        let mut service = result.service_arguments(
            &result.proxy_unit,
            remaining_build_time(context.deadline)?,
            false,
        );
        let podman = service
            .iter()
            .position(|value| value == "/usr/bin/podman")
            .unwrap();
        service.insert(
            podman,
            format!(
                "--property=StandardError=append:{}",
                result.staging.join("build-egress.stderr").display()
            ),
        );
        service.extend(arguments);
        let started = result.runner.run_cancellable(
            Program::SystemdRun,
            &service,
            phase_time(context.deadline, Duration::from_secs(30))?,
            context.cancelled,
        )?;
        if !started.success {
            return Err(network_boundary_error(
                FailureStage::EgressServiceStart,
                &started,
            ));
        }
        // systemd confirms exec, not container readiness. Probe the actual
        // deny boundary while Podman creates its network and starts the helper.
        let probe_deadline = context
            .deadline
            .min(Instant::now() + Duration::from_secs(10));
        loop {
            let probed = result.run_cancellable(
                &[
                    "exec".to_owned(),
                    result.proxy_name.clone(),
                    "/vonk-build-egress".to_owned(),
                    "--probe".to_owned(),
                ],
                remaining_build_time(probe_deadline)?,
                context.cancelled,
            )?;
            if probed.success {
                break;
            }
            if Instant::now() + Duration::from_millis(100) >= probe_deadline {
                return Err(result.readiness_error(probed));
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        Ok(result)
    }

    pub(super) fn address(
        &self,
        network: &str,
        deadline: Instant,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<std::net::Ipv4Addr, RecipeBuildError> {
        let output = self.run_cancellable(
            &[
                "inspect".to_owned(),
                "--format".to_owned(),
                format!("{{{{(index .NetworkSettings.Networks \"{network}\").IPAddress}}}}"),
                self.proxy_name.clone(),
            ],
            phase_time(deadline, Duration::from_secs(10))?,
            cancelled,
        )?;
        if !output.success {
            return Err(network_boundary_error(FailureStage::EgressAddress, &output));
        }
        std::str::from_utf8(&output.stdout)
            .ok()
            .and_then(|text| text.trim().parse().ok())
            .ok_or(RecipeBuildError::NetworkPolicy)
    }

    pub(super) fn readiness_error(
        &self,
        mut probe: crate::process::ProcessOutput,
    ) -> RecipeBuildError {
        // The foreground service's stderr contains OCI startup failures that
        // `podman exec --probe` can only report as "container is not running".
        // Read only a bounded tail of this operation-private diagnostic file.
        if let Ok(mut file) = File::open(self.staging.join("build-egress.stderr")) {
            let _ = file
                .seek(SeekFrom::End(-4096))
                .or_else(|_| file.seek(SeekFrom::Start(0)));
            let mut tail = Vec::new();
            if file.take(4096).read_to_end(&mut tail).is_ok() {
                probe.stderr.push(b'\n');
                probe.stderr.extend(tail);
            }
        }
        network_boundary_error(FailureStage::EgressReadiness, &probe)
    }

    pub(super) fn service_arguments(
        &self,
        unit: &str,
        timeout: Duration,
        wait: bool,
    ) -> Vec<String> {
        let mut arguments =
            podman_user_service_arguments(unit, self.runroot, self.staging, timeout, wait);
        arguments.extend([
            "--property=MemoryMax=402653184".to_owned(),
            "--property=CPUQuota=100%".to_owned(),
            "--property=TasksMax=128".to_owned(),
            "/usr/bin/podman".to_owned(),
        ]);
        arguments.extend(podman_storage_arguments(self.storage, self.runroot));
        arguments
    }

    pub(super) fn arguments(&self, timeout: Duration) -> Vec<String> {
        self.service_arguments(
            &format!("vonk-recipe-build-{}", Uuid::new_v4()),
            timeout,
            true,
        )
    }

    pub(super) fn run(
        &self,
        extra: &[String],
        timeout: Duration,
    ) -> Result<crate::process::ProcessOutput, ProcessError> {
        let mut arguments = self.arguments(timeout);
        arguments.extend_from_slice(extra);
        self.runner.run(Program::SystemdRun, &arguments, timeout)
    }

    pub(super) fn run_cancellable(
        &self,
        extra: &[String],
        timeout: Duration,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<crate::process::ProcessOutput, ProcessError> {
        let mut arguments = self.arguments(timeout);
        arguments.extend_from_slice(extra);
        self.runner
            .run_cancellable(Program::SystemdRun, &arguments, timeout, cancelled)
    }

    pub(super) fn run_with_file(
        &self,
        extra: &[&str],
        path: &Path,
        filesystem: &Path,
        minimum_free_bytes: u64,
        timeout: Duration,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<crate::process::ProcessOutput, ProcessError> {
        let mut arguments = self.arguments(timeout);
        arguments.extend(extra.iter().map(|value| (*value).to_owned()));
        let file = File::open(path)?;
        self.runner.run_with_input_disk_reserve_cancellable(
            Program::SystemdRun,
            &arguments,
            timeout,
            &file,
            ProcessDiskReserve::new(filesystem, minimum_free_bytes),
            cancelled,
        )
    }
}

impl<R: ProcessRunner + ?Sized> Drop for BuildEgress<'_, R> {
    fn drop(&mut self) {
        let deadline = Instant::now() + Duration::from_secs(10);
        // Let Podman stop the container before stopping its owning service;
        // otherwise systemd waits its full stop timeout for conmon first.
        let _ = self.run(
            &[
                "stop".to_owned(),
                "--time=1".to_owned(),
                self.proxy_name.clone(),
            ],
            deadline
                .saturating_duration_since(Instant::now())
                .min(Duration::from_secs(2)),
        );
        let _ = self.runner.run(
            Program::Systemctl,
            &[
                "--user".to_owned(),
                "--no-block".to_owned(),
                "stop".to_owned(),
                self.proxy_unit.clone(),
            ],
            deadline
                .saturating_duration_since(Instant::now())
                .min(Duration::from_secs(2)),
        );
        for arguments in [
            vec![
                "rm".to_owned(),
                "--force".to_owned(),
                self.proxy_name.clone(),
            ],
            vec![
                "network".to_owned(),
                "rm".to_owned(),
                "--force".to_owned(),
                self.internal_network.clone(),
            ],
            vec![
                "network".to_owned(),
                "rm".to_owned(),
                "--force".to_owned(),
                self.outbound_network.clone(),
            ],
            vec![
                "image".to_owned(),
                "rm".to_owned(),
                "--force".to_owned(),
                self.image.clone(),
            ],
        ] {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }
            let _ = self.run(&arguments, remaining.min(Duration::from_secs(2)));
        }
    }
}

pub(super) fn network_boundary_error(
    stage: FailureStage,
    output: &crate::process::ProcessOutput,
) -> RecipeBuildError {
    RecipeBuildError::NetworkBoundary {
        stage,
        diagnostic: podman_build_diagnostic(output),
        logs: Box::new(sanitized_process_logs(output)),
    }
}

/// Rootless container commands must originate in the user manager's clean
/// mount namespace, including the commands that create Podman's pause process.
/// Use an operation-private runtime directory consistently for build and egress.
/// Podman 4.9 replaces /run inside its rootless network namespace and preserves
/// only XDG_RUNTIME_DIR. Its runroot must therefore be inside that directory,
/// not its parent, or Netavark loses access to networks/ipam.db.
pub(super) fn podman_user_service_arguments(
    unit: &str,
    runroot: &Path,
    staging: &Path,
    timeout: Duration,
    wait: bool,
) -> Vec<String> {
    let mut arguments = vec![
        "--user".to_owned(),
        "--collect".to_owned(),
        "--quiet".to_owned(),
        "--service-type=exec".to_owned(),
        format!("--unit={unit}"),
        "--setenv=HOME=/var/lib/vonk-forge-agent".to_owned(),
        "--setenv=XDG_CONFIG_HOME=/var/lib/vonk-forge-agent/.config".to_owned(),
        "--setenv=XDG_DATA_HOME=/var/lib/vonk-forge-agent".to_owned(),
        format!("--setenv=XDG_RUNTIME_DIR={}", runroot.display()),
        format!(
            "--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{}/bus",
            rustix::process::geteuid().as_raw()
        ),
        format!(
            "--setenv=TMPDIR={}",
            staging.join("podman-image-tmp").display()
        ),
        "--setenv=CONTAINERS_STORAGE_CONF=/etc/vonk-forge-agent/containers-storage.conf".to_owned(),
        format!("--property=RuntimeMaxSec={}s", timeout.as_secs_f64()),
        "--property=TimeoutStopSec=5s".to_owned(),
        "--property=KillMode=control-group".to_owned(),
    ];
    if wait {
        arguments.extend(["--wait".to_owned(), "--pipe".to_owned()]);
    }
    arguments
}

pub(super) fn write_proxy_rootfs(
    binary: &Path,
    destination: &Path,
) -> Result<(), RecipeBuildError> {
    let descriptor = rustix::fs::open(
        binary,
        rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .map_err(std::io::Error::from)?;
    let mut source = File::from(descriptor);
    let metadata = source.metadata()?;
    if !metadata.is_file()
        || metadata.uid() != 0
        || metadata.nlink() != 1
        || !(64..=MAX_EGRESS_BINARY_BYTES).contains(&metadata.len())
        || metadata.permissions().mode() & 0o022 != 0
        || metadata.permissions().mode() & 0o111 == 0
    {
        return Err(RecipeBuildError::NetworkPolicy);
    }
    let destination = File::create(destination)?;
    let mut archive = tar::Builder::new(destination);
    let mut header = tar::Header::new_gnu();
    header.set_size(metadata.len());
    header.set_mode(0o555);
    header.set_uid(0);
    header.set_gid(0);
    header.set_mtime(0);
    header.set_cksum();
    archive.append_data(&mut header, "vonk-build-egress", &mut source)?;
    archive.finish()?;
    Ok(())
}

#[cfg(test)]
mod tests {

    #[test]
    #[ignore = "requires an isolated Linux systemd host, agent user and installed egress helper"]
    fn real_egress_boundary_survives_hardened_agent_and_cleans_up() {
        use super::{BuildEgress, BuildEgressStart};
        use crate::process::{ProcessRunner, Program, SystemProcessRunner};
        use std::{
            fs,
            path::Path,
            time::{Duration, Instant},
        };

        let staging = tempfile::tempdir_in("/var/lib/vonk-forge-agent").unwrap();
        let runtime = tempfile::Builder::new()
            .prefix("e-")
            .tempdir_in("/run/vonk-forge-agent")
            .unwrap();
        let storage = staging.path().join("storage");
        fs::create_dir(&storage).unwrap();
        fs::create_dir(staging.path().join("podman-image-tmp")).unwrap();
        let runner = SystemProcessRunner;
        let hosts = ["pypi.org".to_owned()];
        let boundary = BuildEgress::start(
            &runner,
            BuildEgressStart {
                storage: &storage,
                runroot: runtime.path(),
                staging: staging.path(),
                binary: Path::new("/usr/lib/vonk-forge/vonk-build-egress"),
                operation_id: uuid::Uuid::new_v4(),
                hosts: &hosts,
                minimum_free_disk_bytes: 0,
                deadline: Instant::now() + Duration::from_secs(60),
                cancelled: &|| false,
            },
        )
        .unwrap_or_else(|error| panic!("{:?}", error.failure_evidence()));
        let unit = boundary.proxy_unit.clone();
        let active = runner
            .run(
                Program::Systemctl,
                &["--user".to_owned(), "is-active".to_owned(), unit.clone()],
                Duration::from_secs(10),
            )
            .unwrap();
        assert!(active.success, "the proxy must remain owned by its service");
        // Exercise actual Dockerfile RUNs through the production command
        // builder, not just a healthy proxy in its own network namespace.
        let probe = "/usr/lib/vonk-forge/build-network-probe";
        let outbound = format!(
            "{}:18080",
            boundary
                .address(
                    &boundary.outbound_network,
                    Instant::now() + Duration::from_secs(10),
                    &|| false
                )
                .unwrap()
        );
        let reachable = boundary
            .run(
                &[
                    "unshare".to_owned(),
                    "--rootless-netns".to_owned(),
                    probe.to_owned(),
                    "--reachable".to_owned(),
                    outbound.clone(),
                ],
                Duration::from_secs(10),
            )
            .unwrap();
        assert!(
            reachable.success,
            "the outbound canary must be reachable outside the build: {reachable:?}"
        );
        let context = staging.path().join("context");
        fs::create_dir(&context).unwrap();
        fs::copy(probe, context.join("probe")).unwrap();
        fs::write(
            context.join("Dockerfile"),
            "FROM scratch\nCOPY probe /probe\nRUN [\"/probe\"]\nRUN [\"/probe\"]\n",
        )
        .unwrap();
        let proxy = format!(
            "http://{}:18080",
            boundary
                .address(
                    &boundary.internal_network,
                    Instant::now() + Duration::from_secs(10),
                    &|| false
                )
                .unwrap()
        );
        for network in [
            Some((boundary.internal_network.as_str(), proxy.as_str())),
            None,
        ] {
            let mut command = super::podman_user_service_arguments(
                &format!("vonk-recipe-build-{}", uuid::Uuid::new_v4()),
                runtime.path(),
                staging.path(),
                Duration::from_secs(30),
                true,
            );
            command.push("/usr/bin/podman".to_owned());
            command.extend(super::podman_build_arguments(
                &storage,
                runtime.path(),
                network,
            ));
            command.extend([
                "--no-cache".to_owned(),
                "--pull=never".to_owned(),
                "--cap-drop=all".to_owned(),
                "--security-opt=no-new-privileges".to_owned(),
                "--env".to_owned(),
                format!("VONK_TEST_OUTBOUND={outbound}"),
                "--env".to_owned(),
                format!(
                    "VONK_TEST_NETWORK={}",
                    if network.is_some() { "public" } else { "none" }
                ),
                context.display().to_string(),
            ]);
            let built = runner
                .run(Program::SystemdRun, &command, Duration::from_secs(30))
                .unwrap();
            assert!(
                built.success,
                "Dockerfile RUN failed: {}\n{}",
                String::from_utf8_lossy(&built.stdout),
                String::from_utf8_lossy(&built.stderr)
            );
        }
        let mut network_exists = boundary.arguments(Duration::from_secs(10));
        network_exists.extend([
            "network".to_owned(),
            "exists".to_owned(),
            boundary.internal_network.clone(),
        ]);
        drop(boundary);
        assert!(
            !runner
                .run(
                    Program::Systemctl,
                    &["--user".to_owned(), "is-active".to_owned(), unit],
                    Duration::from_secs(10)
                )
                .unwrap()
                .success
        );
        assert!(
            !runner
                .run(
                    Program::SystemdRun,
                    &network_exists,
                    Duration::from_secs(10)
                )
                .unwrap()
                .success
        );
    }
}
