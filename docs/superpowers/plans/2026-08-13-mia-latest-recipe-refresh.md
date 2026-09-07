# Latest Mia Two-Spark Recipe Implementation Plan

> Historical plan retained for provenance. Recipe documents and source
> contexts are authored in the sibling `vonk-forge-recipes` checkout.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans or superpowers:subagent-driven-development.

**Goal:** Add and physically qualify the latest official Mia TP=2 lane through
Vonk's source-first Rust-agent recipe workflow.

**Architecture:** Add one tightly compiled direct-fabric Docker mode, build an
immutable patched runtime from the pinned public Mia image, download the exact
model snapshot as a separate artifact, and orchestrate both ranks through the
existing mapping/build/distribution/install/run lifecycle.

## Constraints

- Preserve the old accepted Mia adapter and package lock byte-for-byte.
- No mutable tags, runtime downloads, runtime patching, SSH orchestration,
  autostart, embedded weights, or credentials.
- Host mode is valid only for connected multi-node profiles and expands to one
  exact helper-validated Docker argument set.
- Keep root filesystem read-only, numeric non-root execution, dropped
  capabilities, no-new-privileges, bounded resources, and exact image identity.

### Task 1: Specify direct-fabric host mode

**Modify:** `schemas/global/recipe-v1.schema.json`,
`control/src/vonk_control/recipe_contract.py`,
`control/tests/test_recipe_contract.py`,
`rust/crates/vonk-agent/tests/workloads.rs`,
`rust/crates/vonk-agent-helper/tests/authority.rs`.

- [x] Add failing tests that accept host mode only for connected multi-node
  profiles, compile the exact host/IPC/InfiniBand/memlock Docker shape, omit
  port publication, and reject incomplete or extra privilege.
- [x] Run focused Python and Rust tests and confirm the intended failures.

### Task 2: Implement the compiled capability

**Modify:** `rust/crates/vonk-agent/src/workloads.rs`,
`rust/crates/vonk-agent/src/oci.rs`,
`rust/crates/vonk-agent-helper/src/operations.rs`, schema mirrors and locks.

- [x] Permit the typed field after semantic validation.
- [x] Require exact multi-node placement for host mode.
- [x] Emit only the fixed direct-fabric arguments.
- [x] Teach the helper parser to accept that complete shape and no variants.
- [x] Regenerate global schema mirrors/contract locks with repository tooling.
- [x] Make the focused tests pass.

### Task 3: Add the immutable Mia recipe

**Create:** `config/recipes/development/mia-deepseek-v4-flash.json`,
`config/recipes/development/mia-deepseek-v4-flash-*.json`, and
`config/recipes/development/mia-deepseek-v4-flash-context/**`.

- [x] Add failing recipe tests for exact upstream/model/image identity,
  two-rank TP=2 topology, model byte count, hotfix inventory/order, networkless
  build, non-root image, offline launcher, and no secrets.
- [x] Vendor the selected Apache-2.0 upstream hotfixes and exact model encoder.
- [x] Build-patch the pinned image and add the rank-aware launcher.
- [x] Generate canonical source/artifact/topology metadata and recipe context
  digest/size.
- [x] Make recipe, source-policy, and runtime tests pass.

### Task 4: Documentation and verification

**Modify:** development workload and fresh-install runbooks; add a Mia audit.

- [x] Document agent upgrade, required disk, model download duration,
  firewall port 8888, import/build/install/run commands, health/inference,
  stop/uninstall, and rollback.
- [x] Run upstream `scripts/ci-validate.sh`, focused suites, full Python CI,
  complete Rust workspace tests, linters, supply-chain scans, and
  `git diff --check`.
- [x] Perform independent security and provenance review and fix findings
  test-first.

### Task 5: Publish and qualify

- [ ] Commit, push, open a PR, require green CI, and merge without creating a
  release.
- [ ] Let GitHub Actions publish the new agent package; update both Sparks from
  the development APT channel.
- [ ] Execute the recipe slices on both nodes, recording new physical evidence.
- [ ] Verify headless worker rendezvous, health, chat, route withdrawal/recovery,
  cleanup, and no residual credentials or unmanaged services.
