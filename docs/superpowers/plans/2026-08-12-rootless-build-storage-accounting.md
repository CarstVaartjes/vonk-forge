# Rootless Build Storage Accounting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep rootless Podman recipe graph roots fully traversable to the parent agent so temporary-storage accounting does not terminate valid Spark builds.

**Architecture:** Add the containers/storage `overlay.force_mask=shared` option to one common operation-private Podman storage argument helper and use it for build, inspect, Docker-archive export, and cleanup. The graph remains unreachable below the existing mode-`0700` agent data root, while `fuse-overlayfs` presents original image permissions inside containers.

**Tech Stack:** Rust, Podman 4.9, containers/storage overlay, fuse-overlayfs, Cargo tests, GitHub Actions signed Debian publication.

## Global Constraints

- Do not skip unreadable graph-root paths or weaken temporary-storage accounting.
- Do not expose Docker/Podman sockets or move source builds into the privileged helper.
- Keep the graph root operation-private and below `/var/lib/vonk-forge-agent`.
- Use the same storage options for every Podman command that opens the graph root.
- Activate packages only through the documented signed A/B canary path.

---

### Task 1: Reproduce and fix rootless graph traversal

**Files:**
- Modify: `rust/crates/vonk-agent/tests/recipe_builder.rs`
- Modify: `rust/crates/vonk-agent/src/recipe_builder.rs`

**Interfaces:**
- Consumes: `RecipeBuilder::build`, `ProcessRunner`, and operation-private `storage`/`runroot` paths.
- Produces: `podman_storage_arguments(storage: &Path, runroot: &Path) -> Vec<String>` used by all four Podman invocations.

- [ ] **Step 1: Write the failing regression assertion**

Add `overlay.force_mask=shared` to the per-call required storage options in `build_exports_a_docker_load_archive_from_the_rootless_builder`. This assertion names the production requirement directly and fails while any Podman command omits the mask.

- [ ] **Step 2: Verify the regression is red**

Run:

```bash
cargo test -p vonk-agent --test recipe_builder build_exports_a_docker_load_archive_from_the_rootless_builder -- --exact
```

Expected: FAIL with `every isolated Podman call must preserve overlay.force_mask=shared`.

- [ ] **Step 3: Implement one common storage-argument helper**

Add a helper returning these exact arguments in order:

```rust
fn podman_storage_arguments(storage: &Path, runroot: &Path) -> Vec<String> {
    vec![
        "--root".to_owned(),
        storage.display().to_string(),
        "--runroot".to_owned(),
        runroot.display().to_string(),
        "--storage-opt".to_owned(),
        "overlay.ignore_chown_errors=true".to_owned(),
        "--storage-opt".to_owned(),
        "overlay.mount_program=/usr/bin/fuse-overlayfs".to_owned(),
        "--storage-opt".to_owned(),
        "overlay.force_mask=shared".to_owned(),
    ]
}
```

Use it to initialize/extend arguments for build, inspect, push, and image removal. Do not alter process, network, CPU, memory, output, or temporary-storage limits.

- [ ] **Step 4: Verify green and run the complete crate gate**

Run:

```bash
cargo test -p vonk-agent --test recipe_builder
cargo test -p vonk-agent
cargo fmt --all -- --check
cargo clippy -p vonk-agent --all-targets --all-features -- -D warnings
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit the tested implementation**

```bash
git add rust/crates/vonk-agent/src/recipe_builder.rs rust/crates/vonk-agent/tests/recipe_builder.rs
git commit -m "fix(agent): preserve rootless build accounting"
```

### Task 2: Document the host and redeploy boundaries

**Files:**
- Modify: `docs/runbooks/development-agent-workloads.md`

**Interfaces:**
- Consumes: the private graph-root and mutable-development Compose deployment contracts.
- Produces: reproducible operator checks for rootless build accounting and bounded Caddy/API replacement.

- [ ] **Step 1: Add the paired graph-root boundary**

Document that `overlay.force_mask=shared` is required for exact parent-side accounting, original permissions are presented through `fuse-overlayfs`, and the mode-`0700` `/var/lib/vonk-forge-agent` ancestor prevents other host users from traversing the graph.

- [ ] **Step 2: Add bounded development redeploy behavior**

Document that a single-replica development `Pull`/`Redeploy` can produce temporary Caddy `EOF`, DNS `no such host`, and 502 entries while `control-api` is replaced. Require post-redeploy API readiness, Caddy `getent hosts control-api`, fresh agent inventory, and no continuing errors before workload actions.

- [ ] **Step 3: Verify documentation and commit**

```bash
git diff --check
rg -n "force_mask=shared|no such host|readyz" docs/runbooks/development-agent-workloads.md
git add docs/runbooks/development-agent-workloads.md
git commit -m "docs: explain development build and redeploy boundaries"
```

### Task 3: Publish and physically prove the fix

**Files:**
- Evidence only: `.state/development-acceptance/` (private and ignored)

**Interfaces:**
- Consumes: GitHub Actions signed ARM64 dev package and accepted `:dev` Compose images.
- Produces: a complete physical synthetic acceptance record and clean Spark/NAS state.

- [ ] **Step 1: Push a PR and require all checks**

Push the branch, open a PR, review the exact patch, and merge only after Rust, control, supply-chain, and package gates pass.

- [ ] **Step 2: Install and activate the signed dev package on Spark 1**

Refresh APT, install the exact newly published version, then use the documented canary activation. Verify active binary SHA-256, slot, supervisor generation, service status, inventory freshness, and packaged firewall check.

- [ ] **Step 3: Redeploy the accepted NAS `:dev` images if the control cohort changed**

Use the unchanged Compose file, preserve named volumes, pull, redeploy, and verify all one-shot containers exit 0 and long-lived services are healthy.

- [ ] **Step 4: Run a fresh synthetic lifecycle**

Use a new evidence path and require all twelve states through deterministic inference, normal stop, route withdrawal, and uninstall. Confirm the produced archive contains Docker `manifest.json` and the Spark runtime has no remaining Vonk-managed container.

- [ ] **Step 5: Remove only diagnostic artifacts**

Delete the explicitly named `/tmp/vonk-recipe-debug-022f07f1`, `debug-022f`, and `debug-mask` roots after physical acceptance. Preserve immutable accepted caches and all named NAS volumes.

- [ ] **Step 6: Continue the complete acceptance plan**

Proceed with NAS/supervisor restart synthetic acceptance, real single-node model qualification, two-node failure/recovery/restart, normal cleanup, final documentation audit, and temporary sudo removal.
