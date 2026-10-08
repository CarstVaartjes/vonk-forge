//! Builder.

use super::*;

pub struct RecipeBuilder<'a, R> {
    pub runner: &'a R,
    pub data_root: &'a Path,
    pub runtime_root: &'a Path,
    pub egress_binary: &'a Path,
}

pub(crate) struct PodmanBuildStaging(TempDir);

impl PodmanBuildStaging {
    pub(crate) fn create(root: &Path) -> std::io::Result<Self> {
        Builder::new().prefix("source-").tempdir_in(root).map(Self)
    }

    pub(crate) fn path(&self) -> &Path {
        self.0.path()
    }
}

impl Drop for PodmanBuildStaging {
    fn drop(&mut self) {
        // Rootless Podman may leave overlay layer directories mode 0555. The
        // agent owns this operation-private tree, but TempDir cannot remove a
        // file from a non-writable directory and silently abandons the whole
        // graphroot. Restore owner traversal/removal before TempDir runs. Do
        // not follow symlinks created in container-image metadata.
        let _ = make_owned_directories_removable(self.path());
    }
}

impl<R: ProcessRunner> RecipeBuilder<'_, R> {
    pub fn layout_path(&self, operation_id: Uuid) -> std::path::PathBuf {
        self.data_root
            .join("builds")
            .join(operation_id.to_string())
            .join("image.docker.tar")
    }

    pub fn build(
        &self,
        request: &RecipeBuildRequest,
        operation_id: Uuid,
        archive: &[u8],
    ) -> Result<RecipeBuildEvidence, RecipeBuildError> {
        self.build_cancellable(request, operation_id, archive, &|| false)
    }

    pub fn build_cancellable(
        &self,
        request: &RecipeBuildRequest,
        operation_id: Uuid,
        archive: &[u8],
        cancelled: &dyn Fn() -> bool,
    ) -> Result<RecipeBuildEvidence, RecipeBuildError> {
        if cancelled() {
            return Err(ProcessError::Cancelled.into());
        }
        let deadline =
            Instant::now() + Duration::from_secs(u64::from(request.limits.timeout_seconds));
        let minimum_free_disk_bytes = ensure_build_disk_available(self.data_root, request)?;
        // `slirp4netns` only isolates the host network namespace; it does not
        // enforce a destination allowlist.  Never silently widen a declared
        // host policy into unrestricted egress. Public builds therefore get
        // an operation-private internal network and dual-homed proxy below.
        let network = build_network(&request.network);
        let staging_root = self.data_root.join("build-staging");
        fs::create_dir_all(&staging_root)?;
        let staging = PodmanBuildStaging::create(&staging_root)?;
        let context = staging.path().join("context");
        let storage = staging.path().join("podman-storage");
        let podman_image_tmp = staging.path().join("podman-image-tmp");
        fs::create_dir_all(self.runtime_root)?;
        // Ubuntu 24.04 ships Podman 4.9, which rejects runroot path strings
        // longer than 50 bytes. Keep the durable, storage-accounted graphroot
        // under the build staging tree while putting only Podman's ephemeral
        // runtime metadata in the private systemd RuntimeDirectory.
        let runroot = Builder::new().prefix("b-").tempdir_in(self.runtime_root)?;
        if runroot.path().as_os_str().as_bytes().len() > 50 {
            return Err(RecipeBuildError::Evidence);
        }
        fs::create_dir_all(&storage)?;
        fs::create_dir(&podman_image_tmp)?;
        let source = materialize_source_bundle(archive, &request.source_bundle_sha256, &context)?;
        let policy = inspect_build_source(&source.files, &request.dockerfile);
        if !policy.passed {
            return Err(RecipeBuildError::Policy(policy));
        }
        let source_bases = dockerfile_base_images(&source.files, &request.dockerfile)
            .ok_or(RecipeBuildError::Evidence)?;
        if source_bases
            != request
                .base_images
                .iter()
                .map(|image| image.reference.clone())
                .collect::<Vec<_>>()
        {
            return Err(RecipeBuildError::Evidence);
        }
        self.import_base_images(
            request,
            &storage,
            runroot.path(),
            minimum_free_disk_bytes,
            deadline,
            cancelled,
        )?;
        let egress = match network {
            BuildNetwork::None => None,
            BuildNetwork::Public => Some(BuildEgress::start(
                self.runner,
                BuildEgressStart {
                    storage: &storage,
                    runroot: runroot.path(),
                    staging: staging.path(),
                    binary: self.egress_binary,
                    operation_id,
                    hosts: &request.network.hosts,
                    minimum_free_disk_bytes,
                    deadline,
                    cancelled,
                },
            )?),
        };
        let tag = format!("localhost/vonk/recipe-build-{}", request.build_id);
        // Start the build as a transient user service rather than a scope.
        // A scope inherits this agent service's mount namespace, whose
        // ProtectKernelTunables/ProtectKernelLogs procfs overmounts prevent a
        // rootless OCI runtime from mounting the build container's /proc. The
        // user manager starts a service from its clean mount namespace while
        // cgroupfs children still inherit this resource envelope.
        let proxy = egress
            .as_ref()
            .map(|boundary| {
                boundary
                    .address(&boundary.internal_network, deadline, cancelled)
                    .map(|address| format!("http://{address}:18080"))
            })
            .transpose()?;
        let mut podman_arguments = podman_build_arguments(
            &storage,
            runroot.path(),
            egress
                .as_ref()
                .zip(proxy.as_deref())
                .map(|(boundary, proxy)| (boundary.internal_network.as_str(), proxy)),
        );
        podman_arguments.extend([
            "--no-cache".to_owned(),
            "--pull=never".to_owned(),
            "--platform".to_owned(),
            BUILD_PLATFORM.to_owned(),
            "--file".to_owned(),
            context.join(&request.dockerfile).display().to_string(),
            "--tag".to_owned(),
            tag.clone(),
            "--cap-drop=all".to_owned(),
            "--security-opt=no-new-privileges".to_owned(),
            format!("--ulimit=nproc={0}:{0}", request.limits.processes),
            format!("--format={}", request.options.format),
            format!("--identity-label={}", request.options.identity_label),
            format!("--jobs={}", request.options.jobs),
            format!(
                "--disable-compression={}",
                request.options.layer_compression == "disabled"
            ),
            format!("--layers={}", request.options.layers),
            format!("--no-hostname={}", request.options.no_hostname),
            format!("--no-hosts={}", request.options.no_hosts),
            format!("--omit-history={}", request.options.omit_history),
            format!("--shm-size={}", request.options.shm_bytes),
            format!(
                "--skip-unused-stages={}",
                request.options.skip_unused_stages
            ),
        ]);
        for item in &request.options.additional_contexts {
            podman_arguments.push("--build-context".to_owned());
            podman_arguments.push(format!(
                "{}={}",
                item.name,
                context.join(&item.path).display()
            ));
        }
        for (flag, entries) in [
            ("--annotation", &request.options.annotations),
            ("--label", &request.options.labels),
            ("--layer-label", &request.options.layer_labels),
        ] {
            for item in entries {
                podman_arguments.push(flag.to_owned());
                podman_arguments.push(format!("{}={}", item.name, item.value));
            }
        }
        for item in &request.options.environment {
            podman_arguments.push("--env".to_owned());
            podman_arguments.push(format!("{}={}", item.name, scalar(&item.value)?));
        }
        if let Some(ignorefile) = &request.options.ignorefile {
            podman_arguments.push("--ignorefile".to_owned());
            podman_arguments.push(context.join(ignorefile).display().to_string());
        }
        for feature in &request.options.os_features {
            podman_arguments.push("--os-feature".to_owned());
            podman_arguments.push(feature.clone());
        }
        if let Some(version) = &request.options.os_version {
            podman_arguments.push("--os-version".to_owned());
            podman_arguments.push(version.clone());
        }
        match request.options.squash.as_str() {
            "new" => podman_arguments.push("--squash".to_owned()),
            "all" => podman_arguments.push("--squash-all".to_owned()),
            _ => {}
        }
        if let Some(timestamp) = request.options.timestamp {
            podman_arguments.push(format!("--timestamp={timestamp}"));
        }
        for name in &request.options.unset_environment {
            podman_arguments.push("--unsetenv".to_owned());
            podman_arguments.push(name.clone());
        }
        for name in &request.options.unset_labels {
            podman_arguments.push("--unsetlabel".to_owned());
            podman_arguments.push(name.clone());
        }
        for capability in &request.capabilities {
            podman_arguments.push(format!("--cap-add={capability}"));
        }
        podman_arguments.push(context.display().to_string());
        let timeout = remaining_build_time(deadline)?;
        let mut arguments = podman_user_service_arguments(
            &format!("vonk-recipe-build-{operation_id}"),
            runroot.path(),
            staging.path(),
            timeout,
            true,
        );
        arguments.extend([
            format!("--property=MemoryMax={}", request.limits.memory_bytes),
            format!(
                "--property=CPUQuota={}%",
                u64::from(request.limits.cpu_cores) * 100
            ),
            format!("--property=TasksMax={}", request.limits.processes),
            "/usr/bin/podman".to_owned(),
        ]);
        arguments.extend(podman_arguments);
        let output = self.runner.run_with_disk_reserve_cancellable(
            Program::SystemdRun,
            &arguments,
            timeout,
            ProcessDiskReserve::new(staging.path(), minimum_free_disk_bytes),
            cancelled,
        )?;
        if !output.success {
            return Err(RecipeBuildError::ImageBuild {
                diagnostic: podman_build_diagnostic(&output),
                logs: Some(Box::new(sanitized_process_logs(&output))),
            });
        }
        // The recipe image is the adaptation stage's input, not the final
        // artifact.  Verify the platform it produced, then apply the resolved
        // platform adapter as the one reviewed, ordered step that owns the
        // interface label, the canonical launcher and the runtime user.
        let recipe_inspection = self.inspect_image(&storage, runroot.path(), &tag, cancelled)?;
        inspect_recipe_image(&recipe_inspection.stdout)
            .map_err(|_| RecipeBuildError::ImageInspect)?;
        let adapter_context = staging.path().join("adapter-context");
        fs::create_dir(&adapter_context)?;
        let adapter_containerfile = adapter_context.join("Containerfile");
        write_adapter_containerfile(&adapter_containerfile, &request.adapter)?;
        let final_tag = format!("localhost/vonk/runtime-adapter-{}", request.build_id);
        self.build_adaptation(
            request,
            &storage,
            runroot.path(),
            staging.path(),
            &tag,
            &final_tag,
            &adapter_containerfile,
            &adapter_context,
            operation_id,
            deadline,
            minimum_free_disk_bytes,
            cancelled,
        )?;
        let adapted_inspection =
            self.inspect_image(&storage, runroot.path(), &final_tag, cancelled)?;
        inspect_adapted_image(&adapted_inspection.stdout, &request.adapter)
            .map_err(|_| RecipeBuildError::AdapterInspect)?;
        let build_root = self.data_root.join("builds");
        fs::create_dir_all(&build_root)?;
        let operation_root = build_root.join(operation_id.to_string());
        fs::create_dir(&operation_root)?;
        let layout = self.layout_path(operation_id);
        let digest_file = staging.path().join("image.digest");
        let mut push_arguments = podman_storage_arguments(&storage, runroot.path());
        push_arguments.extend([
            "push".to_owned(),
            "--digestfile".to_owned(),
            digest_file.display().to_string(),
            final_tag.clone(),
            // Spark's supported runtime is Docker. Export a docker-save
            // archive so the privileged helper can use Docker's native
            // load path without exposing the daemon to the rootless builder.
            format!("docker-archive:{}", layout.display()),
        ]);
        let saved = self.runner.run_with_disk_reserve_cancellable(
            Program::Podman,
            &push_arguments,
            remaining_build_time(deadline)?,
            ProcessDiskReserve::new(&operation_root, minimum_free_disk_bytes),
            cancelled,
        )?;
        if !saved.success {
            return Err(RecipeBuildError::ImageExport);
        }
        let image_digest = fs::read_to_string(&digest_file)?.trim().to_owned();
        if image_digest.strip_prefix("sha256:").is_none_or(|digest| {
            digest.len() != 64
                || !digest
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        }) {
            return Err(RecipeBuildError::Evidence);
        }
        let image_metadata = fs::symlink_metadata(&layout)?;
        if !image_metadata.is_file() || image_metadata.file_type().is_symlink() {
            return Err(RecipeBuildError::Evidence);
        }
        let image_bytes = image_metadata.len();
        if image_bytes == 0 {
            return Err(RecipeBuildError::Evidence);
        }
        if image_bytes > request.limits.output_bytes {
            return Err(RecipeBuildError::OutputLimit);
        }
        let oci_layout_sha256 = sha256_file(&layout)?;
        for reference in [final_tag, tag] {
            let mut remove_arguments = podman_storage_arguments(&storage, runroot.path());
            remove_arguments.extend(["image".to_owned(), "rm".to_owned(), reference]);
            let _ = self
                .runner
                .run(Program::Podman, &remove_arguments, Duration::from_secs(60));
        }
        Ok(RecipeBuildEvidence {
            image_bytes,
            image_digest,
            oci_layout_sha256,
        })
    }

    /// Inspect one image in this build's private storage.
    pub(super) fn inspect_image(
        &self,
        storage: &Path,
        runroot: &Path,
        reference: &str,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<crate::process::ProcessOutput, RecipeBuildError> {
        let mut arguments = podman_storage_arguments(storage, runroot);
        arguments.extend([
            "image".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            IMAGE_INSPECT_FORMAT.to_owned(),
            reference.to_owned(),
        ]);
        Ok(self.runner.run_cancellable(
            Program::Podman,
            &arguments,
            Duration::from_secs(60),
            cancelled,
        )?)
    }

    /// Apply the resolved platform adapter to the built recipe image.
    ///
    /// The adaptation is a real ordered build stage, not a recipe shell hook:
    /// it builds from the recipe image through an argument, installs the
    /// platform contract, and carries the adapter identity as labels so the
    /// exported image can be checked against the plan.
    #[allow(clippy::too_many_arguments)]
    pub(super) fn build_adaptation(
        &self,
        request: &RecipeBuildRequest,
        storage: &Path,
        runroot: &Path,
        staging: &Path,
        recipe_reference: &str,
        final_reference: &str,
        containerfile: &Path,
        context: &Path,
        operation_id: Uuid,
        deadline: Instant,
        minimum_free_disk_bytes: u64,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<(), RecipeBuildError> {
        let mut adapter_arguments = podman_build_arguments(storage, runroot, None);
        adapter_arguments.extend([
            "--no-cache".to_owned(),
            "--pull=never".to_owned(),
            "--platform".to_owned(),
            BUILD_PLATFORM.to_owned(),
            "--file".to_owned(),
            containerfile.display().to_string(),
            "--tag".to_owned(),
            final_reference.to_owned(),
            "--build-arg".to_owned(),
            format!("{ADAPTER_RECIPE_IMAGE_ARGUMENT}={recipe_reference}"),
            "--cap-drop=all".to_owned(),
            "--security-opt=no-new-privileges".to_owned(),
            format!("--format={}", request.options.format),
            format!("--label={RUNTIME_INTERFACE_LABEL}={RUNTIME_INTERFACE_LABEL_VALUE}"),
            format!(
                "--label={RUNTIME_ADAPTER_LABEL}={}",
                request.adapter.definition.adapter_id
            ),
            format!(
                "--label={RUNTIME_ADAPTER_DIGEST_LABEL}={}",
                request.adapter.adapter_sha256
            ),
        ]);
        // The adaptation stage chowns the platform directories, so it needs the
        // same Controller-declared build capabilities as the recipe build.  It
        // never gains a capability the recipe build envelope did not admit.
        for capability in &request.capabilities {
            adapter_arguments.push(format!("--cap-add={capability}"));
        }
        adapter_arguments.push(context.display().to_string());
        let timeout = remaining_build_time(deadline)?;
        let mut arguments = podman_user_service_arguments(
            &format!("vonk-runtime-adapter-{operation_id}"),
            runroot,
            staging,
            timeout,
            true,
        );
        arguments.extend([
            format!("--property=MemoryMax={}", request.limits.memory_bytes),
            format!(
                "--property=CPUQuota={}%",
                u64::from(request.limits.cpu_cores) * 100
            ),
            format!("--property=TasksMax={}", request.limits.processes),
            "/usr/bin/podman".to_owned(),
        ]);
        arguments.extend(adapter_arguments);
        let output = self.runner.run_with_disk_reserve_cancellable(
            Program::SystemdRun,
            &arguments,
            timeout,
            ProcessDiskReserve::new(staging, minimum_free_disk_bytes),
            cancelled,
        )?;
        if !output.success {
            return Err(RecipeBuildError::AdapterBuild {
                diagnostic: podman_build_diagnostic(&output),
                logs: Some(Box::new(sanitized_process_logs(&output))),
            });
        }
        Ok(())
    }
}

impl<R: ProcessRunner> RecipeBuilder<'_, R> {
    pub(super) fn import_base_images(
        &self,
        request: &RecipeBuildRequest,
        storage: &Path,
        runroot: &Path,
        minimum_free_disk_bytes: u64,
        deadline: Instant,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<(), RecipeBuildError> {
        if request.base_images.is_empty() {
            return Ok(());
        }
        let store = BaseImageStore::open(self.data_root).map_err(recipe_base_image_error)?;
        let mut archive_bytes = 0_u64;
        for image in &request.base_images {
            let remaining = request
                .base_image_storage_bytes
                .checked_sub(archive_bytes)
                .ok_or(RecipeBuildError::OutputLimit)?;
            let mut archive = store
                .materialize(
                    self.runner,
                    image,
                    BUILD_PLATFORM,
                    remaining,
                    request.limits.temporary_bytes,
                )
                .map_err(recipe_base_image_error)?;
            archive_bytes = archive_bytes
                .checked_add(archive.bytes)
                .ok_or(RecipeBuildError::Evidence)?;
            if archive_bytes > request.base_image_storage_bytes {
                return Err(RecipeBuildError::OutputLimit);
            }
            archive.file.seek(SeekFrom::Start(0))?;
            let mut load_arguments = podman_storage_arguments(storage, runroot);
            load_arguments.extend(["load".to_owned(), "--quiet".to_owned()]);
            let loaded = self
                .runner
                .run_with_input_disk_reserve_cancellable(
                    Program::Podman,
                    &load_arguments,
                    remaining_build_time(deadline)?,
                    &archive.file,
                    ProcessDiskReserve::new(
                        storage.parent().ok_or(RecipeBuildError::Evidence)?,
                        minimum_free_disk_bytes,
                    ),
                    cancelled,
                )
                .map_err(podman_import_process_error)?;
            if !loaded.success {
                return Err(RecipeBuildError::BaseImageImport {
                    diagnostic: podman_import_diagnostic(&loaded),
                    logs: Some(Box::new(sanitized_process_logs(&loaded))),
                });
            }
            let mut inspect_arguments = podman_storage_arguments(storage, runroot);
            inspect_arguments.extend([
                "image".to_owned(),
                "inspect".to_owned(),
                "--format".to_owned(),
                "{{.Digest}}\t{{.Os}}\t{{.Architecture}}".to_owned(),
                image.reference.clone(),
            ]);
            let inspected =
                self.runner
                    .run(Program::Podman, &inspect_arguments, Duration::from_secs(60))?;
            inspect_base_image(&inspected, &image.manifest_digest)
                .map_err(|_| RecipeBuildError::BaseImageInspect)?;
        }
        Ok(())
    }
}
