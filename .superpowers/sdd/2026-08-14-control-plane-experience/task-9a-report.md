# Task 9A — durable telemetry rollups, retention, and bounded maintenance

## Status

`DONE_WITH_CONCERNS`

Task 9A is implemented and committed locally on
`work/control-plane-frontend-ux`. The affected SQLite suites, migration tests,
Fleet behavior, Worker behavior, compile checks, changed-file Ruff 0.16.1, and
diff checks pass. Docker is not installed on this host, so the Docker-gated
PostgreSQL race tests compile and collect but could not execute. The repository's
full control suite also contains pre-existing Linux-only and mandatory-Docker
tests that cannot pass on this Darwin host; these failures are detailed below.

No push was performed.

## Commits

- Base: `d363a2287fb26662d1a663cfa7b78c7799105b03`
- Code, migration, and tests:
  `df465484747f13ef5e09d25567b26c8e114e8e90`
  (`feat(control): add durable telemetry maintenance`)
- This report is committed separately after the code commit.

## Implemented scope

### Schema and migration

- Added linear Alembic revision `0025_telemetry_retention` after
  `0024_fleet_stream_events`.
- Added matching SQLAlchemy models for:
  - `node_telemetry_rollup_buckets`;
  - `node_telemetry_rollup_metrics`;
  - `node_telemetry_rollup_dirty`.
- Added composite identities, exact 60/900 resolution checks, signed-bigint
  count bounds, bounded metric names, finite ordered metric-value checks,
  timezone-aware timestamp declarations, cascading node/bucket foreign keys,
  and deterministic maintenance indexes.
- Added migration-head, exact-chain, upgrade, downgrade, foreign-key, index,
  check, and migration/model parity coverage.

### Transactional dirty work and rollups

- Newly inserted raw telemetry marks its UTC 60-second bucket dirty in the
  ingestion transaction. Exact replay returns before dirty marking, and
  ingestion rollback also rolls back the marker.
- Dirty marking uses dialect-specific SQLite/PostgreSQL
  `INSERT ... ON CONFLICT DO NOTHING`.
- Maintenance claims a deterministic bounded key set with one ordered,
  limited `DELETE ... RETURNING`, normalizes returned database timestamps,
  and recomputes returned identities in deterministic key order.
- Claim deletion, recomputation, rollup replacement, and child dirty marking
  occur in the same transaction. A concurrent legal late insert waits on the
  claimed identity and inserts a fresh marker after maintenance commits,
  preserving rerun work.
- Minute buckets use exact UTC half-open `[start, start + 60s)` source ranges.
  They store all-source counts and summed gap evidence, and create independent
  metric rows only where that metric has samples.
- Quarter-hour buckets use minute buckets in exact half-open 900-second ranges.
  Means are weighted by each minute metric's sample count; minima and maxima
  span source extrema; source and gap counts are summed separately.
- Recompute replaces stale metric rows, removes empty buckets, remains
  idempotent, and minute recompute always queues its 900-second parent.

### Retention and Fleet semantics

- Each run captures one aware UTC `now` and rolls up before retention.
- Exact boundaries are:
  - raw telemetry: `observed_at < now - 24h`;
  - minute buckets: `bucket_start < floor(now - 30d, 60s)`;
  - quarter-hour buckets: `bucket_start < floor(now - 365d, 900s)`;
  - Fleet events: `expires_at <= now`.
- Every category selects candidates in a deterministic order and deletes no
  more than `delete_limit` per run. Composite and scalar deletes are internally
  chunked to keep SQL parameter counts bounded.
- Raw rows whose minute identity remains dirty are retained. Minute source
  buckets whose 900-second parent remains dirty are retained.
- Expired latest pointers are locked and deleted before their raw rows in the
  same transaction. Before deletion, maintenance appends a bounded
  `node-telemetry` event that references the removed sample.
- Fleet replay proves that the deliberately missing referenced sample triggers
  the existing authoritative `missing-telemetry-sample` snapshot reset at a
  cursor beyond the removal event, clearing latest telemetry rather than
  emitting invalid sparse state.
- Fleet event pruning never rewrites `FleetEventCursor`; reset-event appends are
  the only maintenance operation that advances its high watermark.
- The brief's accepted behavior remains: a legal late sample may outlive an
  already-pruned raw row by up to five minutes and can produce the existing
  missing-sample reset.

### Scheduling

- Added `TelemetryMaintenance.run_once(dirty_limit=512,
  delete_limit=25000)` with strict bounded-limit validation.
- Added an immediate-first-run fixed 15-second aware-clock cadence.
- Missed intervals are skipped without catch-up bursts; non-due calls perform
  only a clock comparison.
- Wired the cadence through the existing `Worker.housekeeping` seam in
  `assemble_production_worker` with the existing session factory and clock.
- Worker round-robin source fairness is preserved because housekeeping does not
  consume or alter the durable-source cursor.

## Transaction and race design

The dirty-key identity is the synchronization point. Ingestion first locks its
node using the existing telemetry lock order, inserts the raw sample, and then
conflict-safely inserts the minute dirty identity in that same transaction.
Maintenance begins with an ordered bounded delete-returning claim. It holds the
deleted identity through recomputation and commit. On SQLite, writer
serialization makes a concurrent marker wait; on PostgreSQL, speculative
conflict handling waits on the deleted unique identity. Once maintenance
commits, the concurrent transaction inserts a new marker rather than losing the
rerun signal.

Retention candidate and lock ordering is deterministic. Latest-pointer reset
work locks affected nodes in sorted order, then affected pointers, appends Fleet
events, deletes pointers, flushes the restrictive foreign keys, and only then
deletes raw samples. Source dirty checks and all retention work share the
maintenance transaction and captured `now`.

## Strict TDD evidence

The implementation was developed in behavior slices, with the new test run
failing before each corresponding production change and passing afterward.
Observed RED evidence included:

- migration/model tests failed because revision 0025 and the three models did
  not exist;
- ingestion tests failed because no rollup dirty marker was written;
- SQL compilation tests failed because the claim statement did not exist;
- minute and quarter tests failed because maintenance/recompute was absent;
- retention tests failed because no bounded pruning occurred;
- latest-pointer tests failed because the pointer/raw pair remained and no
  authoritative reset event was emitted;
- cadence tests failed because the cadence wrapper did not exist;
- deterministic-return-order coverage failed when a simulated
  `DELETE ... RETURNING` result arrived in reverse order.

Each RED was followed by the minimum implementation and a focused GREEN run.
The SQLite concurrency race uses statement hooks and barriers rather than
scheduling sleeps. The equivalent PostgreSQL test uses barriers plus
`pg_blocking_pids`/`pg_stat_activity` lock-state evidence and is Docker-gated.

## Verification

### Passing scoped verification

Command:

```text
uv run --project control --frozen pytest \
  control/tests/test_telemetry.py \
  control/tests/test_telemetry_maintenance.py \
  control/tests/test_telemetry_postgres.py \
  control/tests/test_fleet_events.py \
  control/tests/test_fleet_events_postgres.py \
  control/tests/test_fleet_stream.py \
  control/tests/test_fleet_projection.py \
  control/tests/test_worker.py \
  control/tests/test_production_worker.py \
  control/tests/test_admission_migration.py \
  control/tests/test_migrations.py -q -rs
```

Result: `153 passed, 4 skipped, 18 warnings in 5.74s`.

The four skips are the three telemetry PostgreSQL concurrency cases and the
existing Fleet PostgreSQL ordering case; all report that Docker is required.
The warnings are pre-existing pytest temporary-directory cleanup warnings on
this host.

Additional focused maintenance run after query hardening:
`83 passed, 18 warnings in 3.06s`.

### PostgreSQL-gated verification

Command:

```text
uv run --project control --frozen pytest \
  control/tests/test_telemetry_postgres.py \
  control/tests/test_fleet_events_postgres.py -q -rs
```

Result: `4 skipped in 0.17s`; Docker is unavailable. SQLite and PostgreSQL
claim SQL compilation tests pass, and the SQLite concurrent late-marker race
passes, but the live PostgreSQL lock race was not executable locally.

### Static and compile verification

- Changed Python files with pinned Ruff 0.16.1: `All checks passed!`.
- Python compileall over control source, migrations, and changed tests: exit 0.
- Staged `git diff --cached --check`: exit 0.
- Staged scope audit: exactly 11 control migration/model/telemetry/worker/test
  files; no frontend, API, generated client, Rust, MIA, runtime, readiness, or
  live-system files changed.

Repo-wide Ruff was also attempted. It reports two pre-existing findings in
unchanged `control/tests/test_dev_litellm_database.py` (import ordering and a
`Self` annotation); those findings were not modified as part of Task 9A.

### Broader/full-suite evidence

A broad telemetry/Fleet/worker/migration/agent run produced `260 passed,
4 skipped` and one unrelated failure: `test_invalid_ranges_do_not_leak_artifact_descriptors`
reads Linux `/proc/self/fd`, which does not exist on Darwin.

The complete `control/tests` suite was also attempted and completed with
`1905 passed, 64 skipped, 65 failed, 42 errors`. The failures/errors are
environmental and outside the Task 9A diff: Linux-only `/proc`, `memfd_create`,
and `SO_PEERCRED` behavior on Darwin, plus mandatory PostgreSQL modules that
hard-fail when Docker is absent. The exact Task 9A scoped suite above passes.

## Changed files

- `control/migrations/versions/0025_telemetry_retention.py`
- `control/src/vonk_control/models.py`
- `control/src/vonk_control/telemetry.py`
- `control/src/vonk_control/telemetry_maintenance.py`
- `control/src/vonk_control/worker.py`
- `control/tests/test_admission_migration.py`
- `control/tests/test_production_worker.py`
- `control/tests/test_telemetry.py`
- `control/tests/test_telemetry_maintenance.py`
- `control/tests/test_telemetry_postgres.py`
- `control/tests/test_worker.py`

## Concerns and follow-up

1. Run the Docker-gated telemetry and Fleet PostgreSQL tests on a Docker-capable
   Linux review host before integration. This is the only Task 9A-specific
   verification gap.
2. The full repository suite cannot be green on this Darwin/no-Docker host due
   to existing platform gates. No out-of-scope workaround was added.
3. The requesting-code-review workflow could not dispatch an isolated reviewer
   because this session exposes no subagent facility. A requirement-by-
   requirement self-review was completed; the controller's planned independent
   review remains appropriate.
