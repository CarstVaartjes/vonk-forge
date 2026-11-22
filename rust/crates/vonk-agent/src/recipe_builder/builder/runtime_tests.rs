use super::*;
use std::process::Command;

#[test]
#[ignore = "requires Linux rootless Podman 4.9 and fuse-overlayfs"]
fn fresh_rootless_podman_can_switch_from_agent_to_build_runtime() {
    // Reproduce image import in the agent environment followed by the
    // user service's different XDG_RUNTIME_DIR. An explicit --tmpdir
    // skips Podman's creation of the default rootless pause directory.
    let root = tempfile::tempdir().unwrap();
    let runtime = super::super::budget::owned_directory(root.path(), "r").unwrap();
    let runroot = build_runroot(&runtime, Uuid::new_v4()).unwrap();
    let storage = root.path().join("storage");
    let config = root.path().join("storage.conf");
    fs::write(
            &config,
            format!(
                "[storage]\ndriver=\"overlay\"\nrunroot=\"{}/containers\"\ngraphroot=\"{}/containers/storage\"\n",
                runtime.display(), root.path().display()
            ),
        ).unwrap();
    let arguments = super::super::images::podman_storage_arguments(&storage, &runroot);
    for xdg in [&runtime, &runroot] {
        let output = Command::new("/usr/bin/podman")
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", root.path())
            .env("XDG_DATA_HOME", root.path())
            .env("XDG_CONFIG_HOME", root.path())
            .env("XDG_RUNTIME_DIR", xdg)
            .env("CONTAINERS_STORAGE_CONF", &config)
            .args(&arguments)
            .arg("images")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "rootless runtime {}: status {}; {}",
            xdg.display(),
            output.status,
            String::from_utf8_lossy(&output.stderr)
        );
    }
}
