# Task 4 report — near-real-time Rust agent telemetry

Status: implementation and fix round 1 are complete locally. Nothing was pushed and no pull request was opened.

Implementation commit: `b70f724` (`feat: report near-real-time node telemetry`).

Fix round 1 commit: `6f88e8e` (`fix: persist telemetry sequences across restarts`).

## Scope

The implementation is confined to the Task 4 Rust agent surface:

- `rust/crates/vonk-agent/src/telemetry.rs`
- `rust/crates/vonk-agent/src/lib.rs`
- `rust/crates/vonk-agent/src/client.rs`
- `rust/crates/vonk-agent/src/main.rs`
- `rust/crates/vonk-agent/tests/telemetry.rs`

Fix round 1 changes only `telemetry.rs`, `main.rs`, `tests/telemetry.rs`, and this report. It does not change `state.rs`, OCI/runtime/readiness/workload code, MIA files, controller Python, frontend code, or any live NAS/Spark system.

## Wire compatibility

The agent posts `application/json` to `POST /agent/v1/telemetry`, accepts only HTTP `204`, and preserves the controller's retryable/authentication/protocol status classification. The envelope is exactly `schema_version: 1` plus `samples`; batches contain 1 through 16 samples. The body has no `node_id`, because the authenticated mTLS identity is authoritative.

Every fixed Task 3 sample key is serialized, including nullable metrics as JSON `null`. The client capture tests assert the exact path, content type, envelope/sample key sets, null representation, absence of node identity, 16-sample preflight, and strict success/status handling.

## Collection, cadence, and queue semantics

- The telemetry lane is owned and supervised separately from the claim/result lane. Telemetry retry sleep and retry state cannot gate claim traffic.
- Collection targets one sample every two seconds. The next deadline is based on collection start, so a normal 500 ms collection still targets the original `start + 2s`, not `finish + 2s`.
- When collection spans one or more deadlines, all elapsed slots are skipped and the next strictly future two-second slot is selected. No catch-up samples or bursts are synthesized.
- Static inventory remains on its existing 60-second cadence, and recipe-run observation behavior is unchanged.
- At most 15 unsent samples are retained. Dropping the oldest sample transfers its exact accumulated loss to the oldest retained sample's `gap_samples`. A successful report removes only the sent prefix; retryable failures retain the batch.
- `/proc` and accelerator evidence stays bounded. Optional evidence is represented as `None`, never fabricated zero. The NVIDIA query uses the existing no-shell process runner and a ten-second hard bound.
- Linux `/proc/stat` totals now sum only user through steal. `guest` and `guest_nice` are excluded because Linux already includes them in user/nice.

## Durable sequence and crash semantics

Telemetry uses the existing `state.sqlite` `metadata` table without modifying `state.rs`. One strict JSON value is stored at `telemetry_sequence_v1`:

```json
{"boot_id":"00000000-0000-4000-8000-000000000001","next_unreserved_sequence":64}
```

The allocator opens only an existing regular, non-symlink database in read/write mode. It configures `synchronous=FULL`, foreign keys, a disabled trusted schema, and a one-second busy timeout. Missing/corrupt metadata, malformed or noncanonical boot identity, an unsafe database path, database failure, or sequence exhaustion produces no sample. Startup failure disables the telemetry lane while leaving the independently supervised control lane alive.

Sequences are reserved in blocks of 64 inside an SQLite `IMMEDIATE` transaction. The exclusive high-water mark is durably committed before any sequence in that block can be returned to the collector. Consequently there is no send-before-persist crash window:

- a new boot reserves `[0, 64)` before exposing sequence `0`;
- a same-boot process restart reads the committed high-water mark, reserves `[64, 128)`, and starts at `64`;
- a kernel reboot with a new validated boot ID resets the sequence to `0` and atomically replaces the stored reservation;
- a crash may skip the unused suffix of one reservation block, but it cannot reuse a sequence. Up to 63 values may be skipped per process restart, and the controller contract permits monotonic gaps.

The service assumes one active agent process per node. Concurrent duplicate agent processes receive disjoint reservation blocks, but their network arrival order is not coordinated; service supervision remains responsible for preventing duplicate agents.

## TDD evidence

Baseline before the repair:

| Command | Result |
|---|---|
| `cargo test -p vonk-agent --test telemetry` | 6 passed |
| `cargo test -p vonk-agent --bin vonk-agent` | 5 passed |

RED/GREEN cycles:

| Phase | Evidence |
|---|---|
| RED — cadence API | The telemetry integration target failed to compile because the new start/finish scheduler contract did not exist. |
| RED — restart, corruption, guest accounting | After the scheduler seam was added, 7 tests passed and 3 failed: same-boot restart returned `0` instead of `64`, corrupt state was accepted, and guest counters changed CPU utilization. |
| GREEN — durable sequence/CPU/cadence | The focused telemetry target passed all 10 tests. |
| RED — production retry seam | The binary test failed to compile because `telemetry_retry_after` did not exist. |
| GREEN — retry/claim isolation | The retryable telemetry failure test passed using the production retry policy and lane supervisor. |
| Defense-in-depth | A regular-file/symlink regression was added; the final focused target passes 11 tests. |

## Final verification

| Command | Result |
|---|---|
| `cargo fmt --check` | Exit 0 |
| `cargo check -p vonk-agent` | Exit 0 |
| `cargo test -p vonk-agent --test telemetry` | 11 passed |
| `cargo test -p vonk-agent --lib` | 29 passed |
| `cargo test -p vonk-agent --bin vonk-agent` | 5 passed |
| `cargo test -p vonk-agent` | All targets pass except the two known platform-specific recipe-builder cases below |
| `cargo test -p vonk-agent -- --skip build_exports_a_docker_load_archive_from_the_rootless_builder --skip build_rejects_a_docker_archive_larger_than_declared_output_limit` | 114 passed, 2 filtered out |
| `git diff --check` | Exit 0 |

The two unfiltered failures are the documented laptop baseline:

- `build_exports_a_docker_load_archive_from_the_rootless_builder`
- `build_rejects_a_docker_archive_larger_than_declared_output_limit`

They fail behind the production 50-byte Podman runroot evidence guard. That guard and its recipe-builder/runtime files were not changed.

## Concerns

1. The claim-isolation regression now uses the production retry-delay policy and lane supervisor, but deliberately does not make a live mTLS request or execute a real claim. Client transport behavior and exact telemetry status classification are covered separately in the library capture tests.
2. Reservation blocks trade at most 63 skipped sequence values after a same-boot process crash for roughly one durable SQLite write every 128 seconds at the nominal cadence. Skips are monotonic and wire-compatible.
3. The full unfiltered Rust package remains red only in the two pre-existing platform-specific recipe-builder tests described above.

## Fix round 2 — isolate telemetry durability from control state

Fix commit: `1d95dea` (`fix: isolate telemetry sequence state`), based on `a4af138`. This commit is local only; it was not pushed and no pull request was opened.

The round 1 design used `state.sqlite`. That description is retained above as historical review context but is superseded by this round: sharing SQLite's single-writer lock allowed a telemetry `BEGIN IMMEDIATE` reservation to delay or fail claim/result transactions with `SQLITE_BUSY`.

Telemetry sequence state now has one owner and one database:

- path: `<data_dir>/telemetry-state.sqlite`;
- table: a telemetry-owned strict `metadata(key, value)` table;
- creation: atomic `create_new` with mode `0600`;
- reopen: regular files only, reset to mode `0600` before SQLite opens;
- unsafe paths: symlinks and files with more than one hard link fail closed;
- database policy: WAL, `synchronous=FULL`, foreign keys enabled, trusted schema disabled, and a one-second telemetry-only busy timeout.

The control `state.sqlite` schema and `StateStore` implementation are unchanged. Telemetry reservations no longer open, query, lock, or write that database. A deterministic regression holds `BEGIN IMMEDIATE` on `telemetry-state.sqlite` while a real `StateStore::begin` claim transaction commits on `state.sqlite`; the control transaction completes within the 500 ms bound instead of waiting for a telemetry lock.

All round 1 reservation/crash semantics remain intact. Additional boundary coverage proves:

- one collector emits sequence `63` and then reserves/emits `64`;
- a final 32-value partial block emits `i64::MAX - 31` through `i64::MAX`, then reports `SequenceExhausted`;
- a one-value final block emits `i64::MAX` exactly once, then reports `SequenceExhausted`;
- corrupt metadata, a symlinked telemetry database, and a hard link to the control database all fail closed;
- the resulting telemetry database has exact Unix mode `0600`.

### Round 2 TDD evidence

| Phase | Evidence |
|---|---|
| RED — independent owner | The focused target failed to compile because the requested `TELEMETRY_STATE_FILENAME` contract did not exist. |
| GREEN — independent database | After implementing safe creation/opening, 15 tests passed; the corrupt-state fixture exposed an outdated insert assumption and was corrected to corrupt the existing telemetry row. All 16 tests then passed. |
| RED — hard-link isolation | `hardlinked_control_database_fails_closed` failed because a second filename for the control inode was accepted. |
| GREEN — inode isolation | Requiring exactly one hard link made the regression pass and prevents filename-level separation from disguising a shared SQLite lock. |

### Round 2 final verification

| Command | Result |
|---|---|
| `cargo fmt --check` | Exit 0 |
| `cargo test -p vonk-agent --test telemetry` | 17 passed |
| `cargo test -p vonk-agent --lib` | 29 passed |
| `cargo test -p vonk-agent --bin vonk-agent` | 5 passed |
| `cargo check -p vonk-agent` | Exit 0 |
| `git diff --check` | Exit 0 |

Round 2 changes exactly `rust/crates/vonk-agent/src/telemetry.rs`, `rust/crates/vonk-agent/tests/telemetry.rs`, and this report. `main.rs` required no change. No `state.rs`, OCI, readiness, workload, MIA, controller, frontend, NAS, or Spark files or systems were changed.

### Round 2 concerns

1. An unreleased development run of round 1 may leave the old `telemetry_sequence_v1` metadata row in `state.sqlite`. Round 2 never reads or locks that row; it is harmless and is intentionally not migrated or deleted from control-owned state.
2. The owner-only check covers the durable database file. SQLite manages transient WAL/shared-memory sidecars while the connection is open; they contain only boot/sequence reservation state and inherit the protected database/directory environment.
