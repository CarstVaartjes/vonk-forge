# Vonk Forge Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every local-agent and control-plane issue from the joint repository review.

**Architecture:** Pin the global OCI policy as a vendored contract, enforce bounded DNS-pinned artifact acquisition at the GPU node boundary, and make admission plus orchestration database-atomic. Gang failure recovery is represented as an ordinary idempotent stop job so existing agent fencing and result projection remain authoritative.

**Tech Stack:** Rust, Podman, curl, oras, Python 3.12, SQLAlchemy 2, PostgreSQL/SQLite tests, FastAPI, React, GitHub Actions.

## Global Constraints

- Runtime v1 is Linux/ARM64, digest-pinned, labeled `ai.vonkforge.runtime-interface=v1`, and configured with an explicit numeric non-root user inside rootless Podman.
- Generic network artifacts use credential-free public HTTPS, pinned validated addresses, bounded redirects, and hard byte ceilings.
- A lifecycle state change, its reservations, parent job, and child operations commit in one database transaction.
- Node locks are acquired in sorted node-ID order.
- Every production behavior change starts with a focused failing regression test.

---

### Task 1: Vendored OCI policy

**Files:** `schemas/global/container-runtime-policy-v1.json`, `schemas/global/contract-lock.json`, `scripts/update-global-contracts`, `scripts/verify-contracts`, `rust/crates/vonk-agent/src/oci.rs`, and their tests.

**Interfaces:** Consumes the global exported policy JSON. Produces a checked vendored contract and `RuntimePolicy` validation used by image inspection.

- [ ] Add a Rust test proving root-configured v1 images pass and non-root or missing-label images fail.
- [ ] Run the focused Rust test and confirm the policy file/loading behavior is missing.
- [ ] Vendor and lock the policy, load it through the existing compile-time contract mechanism, and preserve the rootless-user-namespace rationale in operations documentation.
- [ ] Run the Rust test and contract verification to green.
- [ ] Commit the contract alignment.

### Task 2: Bounded public artifact transport

**Files:** `rust/crates/vonk-agent/src/artifact_transport.rs`, `rust/crates/vonk-agent/src/oci.rs`, `rust/crates/vonk-agent/src/process.rs`, and Rust tests.

**Interfaces:** Produces `ArtifactTransport::download_https(url, destination, maximum_bytes)` and public-host validation shared by HTTP, Hugging Face, and OCI registry acquisition.

- [ ] Add tests proving private/literal/link-local DNS answers, DNS changes, credentialed URLs, excessive redirects, and oversized bodies are rejected while a pinned public HTTPS hop is accepted.
- [ ] Run each focused test and confirm it fails for the intended missing policy or ceiling.
- [ ] Implement URL parsing, public-IP resolution, curl `--resolve` pinning, one-hop redirects, maximum file size, and a cumulative Hugging Face budget; bound OCI staging and reject unsafe registries.
- [ ] Run artifact and OCI tests to green, then run the complete Rust workspace.
- [ ] Commit the artifact hardening.

### Task 3: Atomic acceptance and capacity locking

**Files:** `control/src/vonk_control/install_admission.py`, `run_admission.py`, `recipe_operations.py`, `models.py`, and `control/tests/test_*admission.py`, `test_recipe_operations.py`.

**Interfaces:** Produces `accept_install_in_session(...)` and `accept_run_in_session(...)` methods; orchestration queues parent/child jobs in the same `Session`.

- [ ] Add concurrent-admission tests proving a second run cannot overcommit host/GPU memory and a queueing failure rolls back lifecycle rows and reservations.
- [ ] Run focused tests and confirm the current split transactions and missing memory re-check fail.
- [ ] Move acceptance into caller-owned sessions, lock nodes and current inventories in stable order, recompute every reservation, and enqueue jobs before commit.
- [ ] Run the focused tests and all control tests to green.
- [ ] Commit atomic admission.

### Task 4: Gang-start compensation

**Files:** `control/src/vonk_control/recipe_operations.py`, `control/tests/test_recipe_operations.py`, `rust/crates/vonk-agent/src/oci.rs`, and Rust tests.

**Interfaces:** A terminal failed `recipe.start` creates one deterministic `recipe.stop` cleanup job for all run nodes; agent stop is idempotent for absent containers.

- [ ] Add tests for one successful rank plus one failed rank and for stopping an absent container.
- [ ] Run tests and observe the orphaned-rank and non-idempotent behavior.
- [ ] Queue cleanup transactionally, withdraw routing, retain reservations until stop success, and use Podman's ignore-if-absent removal semantics.
- [ ] Run operation and Rust OCI tests to green.
- [ ] Commit gang cleanup.

### Task 5: Complete local CI coverage

**Files:** `.github/workflows/ci.yml` and any existing test scripts it invokes.

**Interfaces:** CI gates the complete control, Rust agent, web unit/build, contract, and generated-API checks.

- [ ] Add or update workflow validation tests if the repository has a workflow test harness; otherwise validate the workflow with actionlint.
- [ ] Expand jobs without duplicating release-only work and keep dependency installs frozen/locked.
- [ ] Run the same commands locally and confirm all are green.
- [ ] Commit CI coverage.

### Task 6: Repository verification

**Files:** All changed local files.

**Interfaces:** Produces a clean, reviewable feature branch.

- [ ] Run formatters, linters, contract verification, generated API verification, full Python suites, full Rust workspace, and web tests/build.
- [ ] Inspect `git diff --check`, dependency changes, and the complete diff for unrelated edits.
- [ ] Commit any verification-only corrections and push the feature branch.
