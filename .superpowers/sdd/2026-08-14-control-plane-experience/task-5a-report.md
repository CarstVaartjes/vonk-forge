# Task 5A report: durable Fleet event outbox

## Status and commits

Implemented Task 5A only from exact base
`57451ffbdf52759c19ea35630385f6e050141355`.

- Implementation commit: `d89dd815e003bfaa019815e1ba08164b15488f65`
  (`feat: add durable fleet event outbox`).
- Report commit: committed separately after this file was written; its immutable
  hash is returned in the final handoff because a commit cannot contain its own
  hash.

Both commits are local on `work/control-plane-frontend-ux`. Nothing was pushed
and no pull request was opened. No Rust, frontend, telemetry ingestion,
recipe-operation, recipe-route, agent-job, MIA runtime/readiness, live-system,
NAS, or Sparks source file was changed.

## Exact changed files

Implementation commit `d89dd815e003bfaa019815e1ba08164b15488f65`
contains exactly these 13 files:

- `control/migrations/versions/0024_fleet_stream_events.py` — linear 0024
  migration, cursor/outbox constraints and indexes, singleton seed, downgrade.
- `control/src/vonk_control/models.py` — `FleetEventCursor` and
  `FleetStreamEvent`, plus metadata-bootstrap singleton seeding so
  `Base.metadata.create_all()` has data parity with Alembic-created schemas.
- `control/src/vonk_control/fleet_events.py` — bounded event values,
  validation, serialized in-transaction allocation, bounded replay reads, and
  the SQLAlchemy recorder/listener lifecycle.
- `control/src/vonk_control/db.py` — installs the recorder at the production
  `session_factory()` composition seam.
- `control/tests/test_fleet_events.py` — portable repository, recorder,
  transition, redaction, rollback, retention, ordering, and lifecycle tests.
- `control/tests/test_fleet_events_postgres.py` — deterministic disposable
  PostgreSQL cursor-lock/commit-order test.
- `control/tests/test_admission_migration.py` — the one authoritative exact
  0001-through-0024 linear chain/head assertion, 0024 round trip, seed, and
  exact migration/model schema parity.
- `control/tests/test_browser_authentication_migration.py` — historical
  migration now asserts only its own parent.
- `control/tests/test_recipe_catalog_migration.py` — historical migration now
  asserts only its own parent.
- `control/tests/test_recipe_deployment_authority.py` — historical migration
  now asserts only its own parent.
- `control/tests/test_reconciliation_execution_migration.py` — removes the
  stale global-head/full-chain duplicate and retains the 0009 parent contract.
- `control/tests/test_rust_agent_migration.py` — historical migration now
  asserts only its own parent.
- `control/tests/test_workload_package_migration.py` — historical migration
  now asserts only its relevant ancestry/artifacts.

This report is the only file in the report-only follow-up commit.

## Schema and ordering contract

Migration `0024_fleet_stream_events` has exact down revision
`0023_node_telemetry`. It creates and seeds:

- `fleet_event_cursor(singleton_id SMALLINT PRIMARY KEY, last_id BIGINT)` with
  named `singleton_id = 1` and `last_id >= 0` checks and row `(1, 0)`;
- `fleet_stream_events` with the eight contracted columns, the exact three
  public event types, strict positive expiry, an 8,192-byte JSON text bound,
  `(expires_at, id)` and `(node_id, id)` indexes, and no node foreign key.

The migration parity test compares both new reflected tables directly with ORM
metadata for ordered columns, compiled types, nullability, primary-key flags,
named check SQL, and indexes. Migration upgrade/downgrade and singleton seed are
also tested. The model table-creation hook seeds the same singleton only when
SQLAlchemy creates the table; an existing database with a missing/corrupt
singleton still fails recording rather than silently recreating ordering state.

Every writer validates its draft, then executes `SELECT ... FOR UPDATE` on
singleton row 1, increments `last_id`, and inserts the event using the current
SQLAlchemy session connection. Source row, cursor increment, and outbox row are
therefore one transaction. Candidate ordering inside one flush is explicit and
stable by source kind then entity ID.

The PostgreSQL test has two threads and database-call barriers. Writer A
allocates and holds row 1; writer B is observed entering the actual cursor-lock
SELECT and cannot allocate while A remains uncommitted. After the barrier
allows A to commit, results and persisted rows must be exactly IDs 1 then 2.
The test was collected locally but skipped because Docker is unavailable; it is
runnable as written on Docker-capable Linux CI.

## Recorder coverage

The recorder collects new objects and uses SQLAlchemy attribute history for
dirty objects before flush. It renders after flush, when generated UUID defaults
exist, and writes via the source transaction connection.

| Source model | Insert | Relevant transition fields | Event/payload proof |
|---|---:|---|---|
| `NodeTelemetryLatest` | yes | `sample_id` | `node-telemetry`; schema version, node ID, immutable sample ID only |
| `RecipeInstallation` | yes | `state` | `recipe-state`; installation/revision/mapping identity and group state |
| `InstallationNode` | yes | `state`, `installed_bytes` | `recipe-state`; installation/node/rank/role state and bounded byte counts |
| `RecipeRun` | yes | `state`, `route_state` | `recipe-state`; run/install/mapping identity, alias, run and route state |
| `RunNode` | yes | `state`, `observed_memory_bytes` | `recipe-state`; run/node/rank/role state and reserved/observed memory |
| `Job` | yes | `state` | `operation-state`; job kind/state and target count only |
| `AgentOperation` | yes | `state`, `current_attempt` | `operation-state`; operation/job/node identity, kind/state/attempt |

Portable tests insert and transition all seven sources, assert IDs/event types,
and assert the important payload literals. An integration test uses the real
`TelemetryRepository`: advancing latest emits once, exact replay emits nothing,
an older sample on another boot is persisted without an event, and conflicting
replay fails without an event. Timestamp-only writes and assigning the exact
current telemetry pointer emit nothing.

## Payload and retention guarantees

- Recorder payloads are constructed from explicit public-field allowlists; no
  source actor, request/job payload, plan, result, evidence digest, endpoint,
  route error, status reason, repository content, or credential is copied.
- Generic draft validation recursively rejects secret-, credential-, token-,
  password-, actor-, request-, evidence-, endpoint-, repository-, and
  error-like keys.
- Payloads must be a finite JSON object. Writer validation uses the normal JSON
  representation and rejects more than 8,192 encoded bytes before allocating a
  cursor; the database repeats the 8 KiB text bound. Oversized recorder output
  fails after source flush and rolls the source transaction back.
- `event_type` is validated in Python and constrained in SQL to exactly
  `node-telemetry`, `recipe-state`, or `operation-state`.
- Every event receives `expires_at = occurred_at + 24 hours`. `after()` returns
  only `id > last_id`, unexpired rows, ascending, with a hard `1..128` limit.
  Expired rows remain physically present but are semantically excluded.
- `retention_window()` obtains committed high watermark and first retained ID
  in one bounded SQL statement/one database snapshot.

## Listener lifecycle and atomicity

`FleetEventRecorder.install(sessionmaker)` stores one recorder on that exact
factory and is idempotent. `uninstall()` removes every registered listener and
factory state. Pending candidates are popped before rendering after flush and
cleared on rollback, soft rollback, and top-level transaction end; tests reuse
a session after failed oversized rendering and close a flushed uncommitted
session without leaking a later draft.

Rollback proofs cover:

- direct append plus source insert and cursor increment all disappearing;
- a forced exception after a source-state flush restoring the old source state,
  event count, and cursor;
- oversized recorder payload failure after source flush leaving no source row,
  outbox row, or cursor advance;
- closed uncommitted sessions rolling source, event, and cursor back together.

## TDD RED/GREEN evidence

| Phase | Command | Observed result |
|---|---|---|
| Baseline migration suite | `pytest` over the initially identified migration tests | `17 passed, 2 skipped, 5 failed`; all five failures were stale `0021` global-head assertions. A sixth stale assertion outside migration-named files was found by the final repository-wide head scan. |
| RED — 0024 | two new focused admission-migration tests | `2 failed`: head was `0023`; revision `0024_fleet_stream_events` did not exist. |
| GREEN — 0024 | same two tests | `2 passed`. |
| RED — ORM models | `test_fleet_event_models_match_the_0024_schema_contract` | `1 failed`: `FleetEventCursor` was absent. |
| GREEN — ORM models | same test | `1 passed`. |
| RED — repository | `pytest control/tests/test_fleet_events.py -q` | collection error: `vonk_control.fleet_events` did not exist. |
| GREEN — repository | same file before recorder tests | `10 passed`. |
| RED — PostgreSQL lock SQL | `test_cursor_allocator_compiles_to_a_postgresql_row_lock` | `1 failed`: the tested lock-statement seam did not exist. |
| GREEN — PostgreSQL lock SQL | lock compilation plus allocation tests | `2 passed`; PostgreSQL dialect output contains `FROM fleet_event_cursor` and `FOR UPDATE`. |
| RED — recorder | same file after recorder specifications | collection error: `FleetEventRecorder` did not exist. |
| GREEN — recorder | same file | `16 passed`. |
| RED — production seam | production-session-factory test | `1 failed`: committed Job produced no event. |
| GREEN — production seam | same test after `db.session_factory` wiring | `1 passed`. |
| Integration RED — metadata seed parity | package/session-factory consumer suites | `9 failed, 8 passed`: metadata-created tables lacked Alembic's singleton seed. The narrowed seam test then failed with a missing row 1. |
| Integration GREEN — metadata seed parity | narrowed seam, then the package/session-factory suites | `1 passed`; then `17 passed`. |
| RED — retention snapshot | `test_retention_window_reads_one_database_snapshot` | `1 failed`: two SELECT statements were observed. |
| GREEN — retention snapshot/bounds | focused repository cases | `6 passed`. |
| RED — deterministic intra-transaction order | same-transaction reversed Job test | `1 failed`: events followed insertion order `job-b`, `job-a`. |
| GREEN — deterministic order plus recorder coverage | deterministic/insert/transition tests | `3 passed`. |

All production behavior was preceded by a focused expected failure. Test
expectations use literal payloads and persisted database effects rather than
mock assertions.

## Final verification

| Command | Result |
|---|---|
| Combined scoped `pytest` over Fleet events/PostgreSQL collection, telemetry, recipe operations/routes, jobs/agent jobs, production session-factory package consumers, all `test_*migration*.py`, and recipe deployment authority | `243 passed, 4 skipped` in 19.59s. Skips: Fleet PostgreSQL ordering, recipe PostgreSQL concurrency, and two PostgreSQL migration cases; all require unavailable Docker. Twelve pre-existing macOS pytest temporary-directory cleanup warnings remained. |
| `pytest control/tests/test_fleet_events_postgres.py -q -rs` | `1 skipped`; exact reason: Docker is required for PostgreSQL Fleet event ordering tests. |
| `uvx --from ruff==0.16.1 ruff check` over all 13 implementation/test files | `All checks passed!` |
| `python -m compileall -q` over Task 5A Python source/migration/tests | Exit 0. |
| `git diff --check` | Exit 0, no whitespace errors. |
| repository-wide `get_heads()` / `get_current_head()` scan | Exactly one assertion remains: authoritative 0024 chain/head in `test_admission_migration.py`. |
| prohibited-source diff scan | No Rust, frontend, telemetry, recipe operation/route, agent job, runtime, or readiness source edits. |

The exact final test command was:

```bash
control/.venv/bin/pytest control/tests/test_fleet_events.py control/tests/test_fleet_events_postgres.py control/tests/test_telemetry.py control/tests/test_recipe_operations.py control/tests/test_recipe_routes.py control/tests/test_jobs.py control/tests/test_agent_jobs.py control/tests/test_package_validation_runner.py control/tests/test_package_services.py control/tests/test_package_action_plans.py control/tests/test_*migration*.py control/tests/test_recipe_deployment_authority.py -q -rs
```

The exact final static verification commands were:

```bash
uvx --from ruff==0.16.1 ruff check control/src/vonk_control/fleet_events.py control/src/vonk_control/db.py control/src/vonk_control/models.py control/migrations/versions/0024_fleet_stream_events.py control/tests/test_fleet_events.py control/tests/test_fleet_events_postgres.py control/tests/test_admission_migration.py control/tests/test_rust_agent_migration.py control/tests/test_workload_package_migration.py control/tests/test_browser_authentication_migration.py control/tests/test_reconciliation_execution_migration.py control/tests/test_recipe_catalog_migration.py control/tests/test_recipe_deployment_authority.py
control/.venv/bin/python -m compileall -q control/src/vonk_control/fleet_events.py control/src/vonk_control/db.py control/src/vonk_control/models.py control/migrations/versions/0024_fleet_stream_events.py control/tests/test_fleet_events.py control/tests/test_fleet_events_postgres.py
git diff --check d89dd815e003bfaa019815e1ba08164b15488f65^ d89dd815e003bfaa019815e1ba08164b15488f65
```

## Self-review and remaining concerns

- Scope: Task 5A foundation only. Projection, hydration, SSE endpoint, resume
  header parsing, snapshots/resets, keepalives, and stream cleanup remain for the
  later Fleet projection/SSE task and are intentionally absent here.
- PostgreSQL correctness is implemented with a real row lock and a deterministic
  integration test, but this host could not execute that test. Docker-capable
  Linux CI must execute `control/tests/test_fleet_events_postgres.py` before the
  ordering proof is considered environment-complete.
- SQLite portable tests prove atomic rollback, deterministic allocation, bounds,
  recorder semantics, and schema parity; SQLite cannot prove PostgreSQL row-lock
  scheduling.
- The full controller suite was not run because it contains unrelated
  platform/Docker cases; the final scoped 243-test set includes every changed
  migration test, each tracked source-service suite, and known production
  session-factory consumers.
- The twelve pytest cleanup warnings are pre-existing macOS temporary-directory
  cleanup warnings and are unrelated to Task 5A behavior.

## Fix Round 1 (Task 5A)

### Status and commits

Fix Round 1 was completed from exact base
`860a5162271a423d05c2fb3e4a96f0dd9df19e9e` on
`work/control-plane-frontend-ux`.

- Source/tests commit: `adffe0321be89d85e91ffae9d63f703000fd13e8`
  (`fix: harden fleet event recording`).
- Report commit: committed separately after this section was written; its
  immutable hash is returned in the final handoff because a commit cannot
  contain its own hash.

Both commits are local. Nothing was pushed and no pull request was opened. The
round did not touch frontend, Rust, MIA/runtime/readiness, or live systems.

### Exact changed files

The source/tests commit contains exactly these eight files:

- `control/src/vonk_control/jobs.py` — preserves the conditional resume winner
  update while also applying the winning transition to the loaded `Job`, so
  SQLAlchemy attribute history records the real queued event in the same
  transaction.
- `control/src/vonk_control/operation_api.py` — applies the same event-producing
  behavior to the durable operation projection's real resume path while
  preserving its `KeyError`/`ValueError` contract.
- `control/src/vonk_control/fleet_events.py` — uses one atomic SQLite
  `UPDATE ... RETURNING` statement to increment and return the cursor; the
  PostgreSQL row-lock allocation path remains unchanged.
- `control/src/vonk_control/models.py` — compiles the payload check as SQLite
  BLOB byte length or PostgreSQL-compatible `octet_length(TEXT)`.
- `control/migrations/versions/0024_fleet_stream_events.py` — creates the same
  dialect-aware UTF-8 byte constraint during migration.
- `control/tests/test_fleet_events.py` — proves both real resume event paths,
  multibyte rejection, PostgreSQL constraint compilation, and concurrent
  SQLite allocation.
- `control/tests/test_admission_migration.py` — proves migrated SQLite rejects a
  payload whose character count is below 8,192 but whose UTF-8 representation
  exceeds 8,192 bytes, and keeps migration/model constraint parity
  dialect-aware.
- `control/tests/test_fleet_events_postgres.py` — replaces a timing-only
  non-allocation assertion with a database-observed lock proof using backend
  PIDs, `pg_stat_activity`, and `pg_blocking_pids`.

### Behavior and concurrency review

Both resume implementations first load and validate a waiting job, then retain
the existing guarded `UPDATE ... WHERE state = 'waiting-for-operator'` as the
single-winner gate. Only a winner mutates the loaded ORM object. Commit then
flushes real `Job.state` attribute history, allowing the installed production
recorder to append the queued event atomically with the state transition. The
guarded SQL update holds its row lock until commit; the later ORM flush is a
second update but cannot lose the winner guarantee.

SQLite does not honor `SELECT ... FOR UPDATE`. Its allocation path now performs
the increment and read as one atomic `UPDATE ... RETURNING`, so two connections
cannot read the same previous cursor value. The portable concurrency test holds
writer A open until writer B has entered that update, then proves committed IDs
and rows are exactly 1 and 2 and the cursor is 2.

The payload constraint now measures encoded bytes rather than characters:
SQLite compiles `length(CAST(payload AS BLOB))`; PostgreSQL compiles
`octet_length(CAST(payload AS TEXT))`. Both the metadata-created and
Alembic-created SQLite schemas reject a multibyte JSON value that is fewer than
8,192 characters but greater than 8,192 UTF-8 bytes.

The PostgreSQL concurrency test now records each writer's backend PID and does
not release writer A until an independent connection observes writer B blocked
by writer A with `wait_event_type = 'Lock'`. It then retains the exact ID,
persisted-row, and cursor assertions.

### TDD RED/GREEN evidence

The following evidence was recorded by the Fix Round 1 implementer before the
final review:

| Phase | Observed result |
|---|---|
| Baseline | `26 passed, 1 skipped`; the skip required Docker. |
| RED — real resume emission | Two new tests failed because each real resume path persisted only the initial `waiting-for-operator` event and no queued event. |
| GREEN — real resume emission | Both new tests passed after exposing the winning ORM transition; both existing concurrent-resume winner tests also passed. |
| RED — UTF-8 database bound | The PostgreSQL metadata compile test produced `length(CAST(payload AS TEXT))`, but required `octet_length(CAST(payload AS TEXT))`. |
| GREEN — UTF-8 database bound | Dialect-aware model and migration constraints passed the PostgreSQL compile and SQLite multibyte rejection/parity cases. |
| Focused implementation run | `64 passed, 1 skipped`, as recorded by the implementer. |

The SQLite atomic-allocation and stronger PostgreSQL lock-proof specifications
were added during the same test-first round. Their standalone RED output was
not retained in the handoff, so no exact failure text is reconstructed here.
The PostgreSQL proof was collected but could not execute without Docker.

### Final verification

| Command/scope | Result |
|---|---|
| Focused Fleet events/PostgreSQL collection, jobs, operation API, and admission migration | `63 passed, 1 skipped` (`64 collected`) in 3.45s. The skip was the Docker-backed PostgreSQL Fleet ordering test. |
| All `test_*migration*.py` plus recipe deployment authority | `52 passed, 2 skipped` in 8.42s. Both skips were Docker-backed PostgreSQL migration tests. |
| Broader scoped Fleet, telemetry, recipe operations/routes, jobs/agent jobs, operation API, package consumers, migrations, and deployment authority | `265 passed, 4 skipped` in 21.25s. |
| Existing `test_concurrent_operator_resume_has_one_winner` and `test_durable_resume_has_one_atomic_winner`, repeated together 10 times | Every repetition passed: `2 passed` each, 20 test executions total. |
| Ruff 0.16.1 over all eight changed source/test files | `All checks passed!` |
| `python -m compileall -q` over all eight changed Python files | Exit 0. |
| `git diff --check` before the source/tests commit | Exit 0, no whitespace errors. |

The broader scoped command was:

```bash
control/.venv/bin/pytest control/tests/test_fleet_events.py control/tests/test_fleet_events_postgres.py control/tests/test_telemetry.py control/tests/test_recipe_operations.py control/tests/test_recipe_routes.py control/tests/test_jobs.py control/tests/test_agent_jobs.py control/tests/test_operation_api.py control/tests/test_package_validation_runner.py control/tests/test_package_services.py control/tests/test_package_action_plans.py control/tests/test_*migration*.py control/tests/test_recipe_deployment_authority.py -q -rs
```

Its four skips were the PostgreSQL Fleet ordering test, PostgreSQL recipe
concurrency test, and two PostgreSQL migration cases. Docker is not installed
on this host. The same twelve pre-existing macOS pytest temporary-directory
cleanup warnings appeared and are unrelated to the round.

### Remaining coverage concerns

- The live PostgreSQL lock-wait proof and the PostgreSQL execution of the
  `octet_length` check remain environment-incomplete until Docker-capable Linux
  CI runs them. Local coverage proves PostgreSQL SQL compilation only.
- SQLite proves its own atomic allocator and transactional results but cannot
  substitute for PostgreSQL row-lock scheduling semantics.
- Successful real resume paths prove queued event emission, and the existing
  concurrency tests prove a single resume winner. There is not yet one combined
  contention test that also asserts exactly one queued outbox event.
- The broader scoped suite was run instead of the entire controller repository;
  it covers every changed source seam and all migration tests in scope.
