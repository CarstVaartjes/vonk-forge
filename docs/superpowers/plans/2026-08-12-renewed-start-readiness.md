# Renewed Start Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make workload readiness honor controller-renewed agent leases and let the private development acceptance runner recover once from an automatically cleaned failed start.

**Architecture:** The agent loop owns a Tokio watch channel initialized with the claim deadline. Valid heartbeat directives publish newer authorized deadlines, and the recipe readiness loop consults the receiver on every poll. The acceptance runner preserves the first failed operation, waits for its run to reach the existing stopped/withdrawn recovery boundary, and creates one deterministic replacement run.

**Tech Stack:** Rust 2024, Tokio watch channels, async-trait, chrono, Python 3.12 standard library, pytest.

## Global Constraints

- Preserve exact artifact verification before every start.
- Never extend a lease locally; only publish deadlines from validated controller directives.
- Preserve fencing, durable terminal results, cancellation, and fail-closed heartbeat behavior.
- Never retry a failed start operation in place.
- Permit at most one replacement run with request purpose `running:start-retry`.
- Preserve the first failed operation and run identifiers in private acceptance evidence.

---

### Task 1: Propagate renewed deadlines into workload readiness

**Files:**
- Modify: `rust/crates/vonk-agent/src/executor.rs`
- Modify: `rust/crates/vonk-agent/src/health.rs`

**Interfaces:**
- Consumes: `AgentDirective.deadline: DateTime<FixedOffset>` after `StateStore::apply_heartbeat` validates and persists the directive.
- Produces: `Executor::execute(&self, claim: &AgentClaim, lease_deadline: watch::Receiver<DateTime<FixedOffset>>) -> ExecutionResult`.
- Produces: `wait_ready(address, port, path, lease_deadline: watch::Receiver<DateTime<FixedOffset>>) -> Result<(), HealthError>`.

- [ ] **Step 1: Write the failing executor-loop test**

Update `HeartbeatGatedExecutor` to receive the lease receiver and retain the
latest deadline it observes. Extend
`long_execution_renews_and_persists_its_lease_before_result` to assert the
executor-observed deadline is greater than the original claim deadline.

- [ ] **Step 2: Run the executor test and verify RED**

Run:

```bash
cargo test --manifest-path Cargo.toml -p vonk-agent long_execution_renews_and_persists_its_lease_before_result -- --exact --nocapture
```

Expected: compilation or assertion failure because `Executor::execute` does
not receive renewed deadlines.

- [ ] **Step 3: Implement the minimal executor lease channel**

Create `tokio::sync::watch::channel(claim.deadline)` before spawning the
heartbeat task. Pass the receiver to the executor. Pass the sender to
`run_heartbeats`, and publish `directive.deadline` only after
`state.apply_heartbeat(&progress, &directive)` succeeds. Update all executor
implementations and test executors to accept the receiver without changing
non-start behavior.

- [ ] **Step 4: Run the executor test and verify GREEN**

Run the command from Step 2. Expected: PASS with the executor and submitted
result both observing a deadline newer than the original claim.

- [ ] **Step 5: Write the failing readiness-extension test**

In `health.rs`, add an async test with a local HTTP listener. Initialize a watch
channel with an already elapsed deadline, publish a future controller deadline,
then call `wait_ready`. Assert the successful local health response is accepted.
The production change that makes this pass is reading the latest receiver value
instead of a copied deadline.

- [ ] **Step 6: Run the health test and verify RED**

Run:

```bash
cargo test --manifest-path Cargo.toml -p vonk-agent readiness_honors_a_controller_renewed_deadline -- --exact --nocapture
```

Expected: compilation or `HealthError::Deadline` because `wait_ready` still
uses a static `DateTime<Utc>`.

- [ ] **Step 7: Implement dynamic readiness deadlines**

Change `wait_ready` to accept the watch receiver and read its current value at
the top of every loop. Update recipe start to pass its executor receiver. Do
not alter poll intervals, health response validation, or artifact verification.

- [ ] **Step 8: Verify the focused and complete Rust agent suites**

Run:

```bash
cargo fmt --all --check
cargo test --manifest-path Cargo.toml -p vonk-agent
cargo clippy --manifest-path Cargo.toml -p vonk-agent --all-targets -- -D warnings
```

Expected: all commands exit 0.

- [ ] **Step 9: Commit the agent fix**

```bash
git add rust/crates/vonk-agent/src/executor.rs rust/crates/vonk-agent/src/health.rs
git commit -m "fix: honor renewed leases during workload start"
```

---

### Task 2: Recover one automatically cleaned failed acceptance run

**Files:**
- Modify: `scripts/run-development-slices`
- Modify: `scripts/tests/test_run_development_slices.py`

**Interfaces:**
- Consumes: run status `state=stopped` and `route_state=withdrawn` from `GET /api/v1/recipes/runs/{run_id}`.
- Produces: one replacement `POST /api/v1/recipes/runs` using `request_key("running", "start-retry")` and a fresh preview digest.
- Produces: evidence fields `failed_run_id` and `failed_run_operation_id` when replacement recovery is used; existing `run_id` and `run_operation_id` identify the successful replacement.
- Produces: restart checkpoints `<purpose>_plan_digest`, `<purpose>_operation_id`, and `<purpose>_run_id` for purposes `start` and `start_retry`.

- [ ] **Step 1: Write the failing successful-recovery test**

Extend `SliceServer` with a start-state sequence and run-state map. Make the
first start operation fail, expose that first run as stopped/withdrawn, and make
the second start succeed. Assert the runner completes through `inference-ok`,
submits exactly two run creations and two previews, uses the UUID derived from
`running:start-retry` on the second creation, and records both failed IDs.

- [ ] **Step 2: Run the recovery test and verify RED**

Run:

```bash
uv run --project control --frozen pytest scripts/tests/test_run_development_slices.py -q -k 'recovers_once_after_cleaned_failed_start'
```

Expected: FAIL because the runner exits on the first failed start.

- [ ] **Step 3: Implement one fresh-run recovery**

Refactor the run submission into a focused helper that previews and creates a
run for a supplied request-key purpose. In `running`, inspect the first terminal
operation without invoking generic operation retry. On failure, poll the first
run until it is exactly stopped/withdrawn, submit one replacement with purpose
`start-retry`, and require normal successful node evidence. Record the original
failed identifiers only after the replacement succeeds.

Before each creation, atomically checkpoint its exact preview digest. After the
response, atomically checkpoint its operation and owner run IDs. If those IDs
already exist on resume, validate their canonical UUIDs and exact digest/owner
association, then poll the operation directly without previewing or creating.
If only the digest exists, replay its exact request tuple first. Accept a
committed idempotent response; only an authoritative stale-digest response with
the request key still unused may replace the checkpoint with one fresh preview
and one submission. Cover committed-response loss and uncommitted interruption
for both `start` and `start-retry`.

- [ ] **Step 4: Run the recovery test and verify GREEN**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Write the failing bounded-recovery test**

Configure both start operations to fail. Assert the runner exits nonzero, makes
exactly two run creations, does not mark `running` complete, and never submits a
third creation or an operation-retry request.

- [ ] **Step 6: Run the bounded-recovery test and verify RED/GREEN**

Run:

```bash
uv run --project control --frozen pytest scripts/tests/test_run_development_slices.py -q -k 'does_not_retry_a_failed_replacement_start'
```

Expected before the final guard: FAIL if more than one replacement is allowed.
Expected after the minimal guard: PASS.

- [ ] **Step 7: Write the failing multi-invocation restart test**

Add a three-invocation regression that interrupts operation polling after the
initial and replacement responses. Change the fake preview digest before each
resume and assert that only two previews and two creations occur in total.

- [ ] **Step 8: Verify the script suite and formatting**

Run:

```bash
uv run --project control --frozen pytest scripts/tests/test_run_development_slices.py -q
uvx --from ruff==0.16.1 ruff check --force-exclude scripts/run-development-slices scripts/tests/test_run_development_slices.py
uvx --from ruff==0.16.1 ruff format --check scripts/run-development-slices scripts/tests/test_run_development_slices.py
```

Expected: all commands exit 0.

- [ ] **Step 9: Commit the acceptance recovery**

```bash
git add docs/superpowers/specs/2026-08-12-renewed-start-readiness-design.md docs/superpowers/plans/2026-08-12-renewed-start-readiness.md scripts/run-development-slices scripts/tests/test_run_development_slices.py
git commit -m "fix: recover cleaned development starts once"
```

---

### Task 3: Verify, publish, and resume physical acceptance

**Files:**
- Modify only if verification exposes a defect in scope.

**Interfaces:**
- Consumes: signed development APT package and existing private physical evidence file.
- Produces: healthy canary and primary Spark agents, successful DS4 inference, and completed single-node lifecycle evidence.

- [ ] **Step 1: Run repository verification proportional to the change**

Run the Rust workspace and control/script gates used by GitHub Actions, inspect
the branch diff, and confirm no private evidence, token, model artifact, or key
is tracked.

- [ ] **Step 2: Push a PR and require GitHub Actions**

Push `fix/renewed-start-readiness`, create the PR, wait for every required check
to pass, and merge without creating a release tag.

- [ ] **Step 3: Activate the signed development agent package**

Wait for the development APT publication workflow. Update Spark 2 first, verify
its exact installed package/runtime identity and healthy heartbeat, then update
Spark 1 and verify the same evidence.

- [ ] **Step 4: Resume physical single-node acceptance**

Run `scripts/run-development-slices` with the existing private evidence file
through `inference-ok`. Confirm the old failed operation remains present, the
replacement run succeeds, and inference returns the exact model acceptance
phrase.

- [ ] **Step 5: Complete restart, stop, and uninstall**

Restart the Spark agent and ordered NAS control services while preserving
volumes, resume the evidence, and complete persistence, stop, route withdrawal,
and uninstall checkpoints.

- [ ] **Step 6: Continue the total rollout plan**

Execute the physical two-node failure/recovery slice, then proceed to the
fresh-install documentation audit, website update, final verification, and
removal of temporary sudo grants.
