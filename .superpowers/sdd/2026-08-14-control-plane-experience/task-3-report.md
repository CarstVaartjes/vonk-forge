# Task 3 report: typed telemetry persistence and ingestion

## Status and scope

Implemented authenticated, bounded control-side telemetry persistence and `POST /agent/v1/telemetry` from base commit `174eb111232ee0ed3c7ed22b41b6e4a524b20f6c`.

The implementation commit is `490df62201defe89972e556323e8f24ac8409fd0` (`feat: ingest bounded node telemetry`). The report is committed separately so it can name the immutable implementation hash; the report commit hash is returned in the final handoff because a commit cannot embed its own final hash.

No Rust, frontend, MIA recipe, MIA runtime, readiness, live-system, push, or pull-request changes were made.

## Changed files

Implementation commit `490df62201defe89972e556323e8f24ac8409fd0`:

- `control/src/vonk_control/telemetry.py` — public frozen telemetry dataclasses, pre-transaction validation, idempotent batch recording, latest projection, and bounded history reads.
- `control/src/vonk_control/models.py` — typed `NodeTelemetrySample` and `NodeTelemetryLatest` ORM models, uniqueness, range/cross-field checks, and history index.
- `control/src/vonk_control/agent_api.py` — strict complete wire models and authenticated telemetry route; the body has no node identity field.
- `control/src/vonk_control/api.py` — endpoint-local streamed 64 KiB boundary and duplicate-key rejection, ordered after trusted-proxy agent authentication and before Pydantic.
- `control/migrations/versions/0023_node_telemetry.py` — linear telemetry tables/index migration and downgrade.
- `control/tests/test_telemetry.py` — repository, model, validation, ordering, idempotency, rollback, and bounded-read tests.
- `control/tests/test_agent_api.py` — authenticated route, identity ownership, exact wire validation, body streaming, duplicate-key, and authentication-before-parse tests.
- `control/tests/test_admission_migration.py` — `0023` linear-head and telemetry table/column assertions.

Report follow-up commit:

- `.superpowers/sdd/2026-08-14-control-plane-experience/task-3-report.md` — this report.

## Exact HTTP and JSON wire contract for Task 4

Request: `POST /agent/v1/telemetry` with a JSON body. Successful ingestion returns `204 No Content`.

The endpoint accepts at most 65,536 encoded body bytes and streams only until the first byte beyond that limit. It does not trust `Content-Length`: absent, falsely small, or truthful length metadata has the same actual-body enforcement. Every duplicate object key at any nesting depth is rejected before Pydantic parsing. Unknown fields are rejected at the envelope, sample, and details levels.

The authenticated certificate/proxy scope is the only source of `node_id`. There is no `node_id` field in the envelope, sample, or details schema. Unauthenticated or inactive-certificate requests return `401` before body streaming, JSON decoding, duplicate-key inspection, or Pydantic parsing. An authenticated body over 64 KiB returns `413`; malformed, duplicate-key, inconsistent, stale, future, or out-of-range authenticated bodies return `422`.

### Envelope

| JSON field | Required | JSON type | Exact constraint |
|---|---:|---|---|
| `schema_version` | yes | integer | Exactly integer `1`; Boolean `true` and number `1.0` are rejected. |
| `samples` | yes | array | 1 through 16 complete sample objects. Global `observed_at` order is strictly increasing. `(boot_id, sequence)` is unique in the batch. For repeated `boot_id` values, `sequence` is strictly increasing. |

### Sample

All fixed core fields below are required. Nullable fields must be emitted as JSON `null` when the collector cannot obtain a value; omission is rejected. JSON numbers must be finite. Integer fields are strict JSON integers and reject Booleans and floats.

| JSON field | JSON type | Exact accepted range/format |
|---|---|---|
| `boot_id` | string | Canonical lowercase, hyphenated, non-nil UUID: `8-4-4-4-12` hexadecimal characters. No UUID version or variant is required. |
| `sequence` | integer | `0..9,223,372,036,854,775,807`, inclusive. |
| `observed_at` | string | RFC 3339 date-time with an explicit UTC offset. After conversion to UTC, inclusive controller acceptance window is `received_at - 5 minutes` through `received_at + 30 seconds`. Numeric Unix timestamps and naive strings are rejected. |
| `cpu_utilization_percent` | number or null | `0..100`, inclusive. |
| `load_average_1m` | number or null | `0..1,000,000`, inclusive. |
| `memory_total_bytes` | integer or null | `0..17,592,186,044,416` (16 TiB), inclusive. Both memory fields are null or both are integers. |
| `memory_available_bytes` | integer or null | Same range; when present, `memory_available_bytes <= memory_total_bytes`. This is `/proc/meminfo` available memory, not `MemFree`. |
| `disk_total_bytes` | integer or null | `0..17,592,186,044,416` (16 TiB), inclusive. Both disk fields are null or both are integers. |
| `disk_free_bytes` | integer or null | Same range; when present, `disk_free_bytes <= disk_total_bytes`. Task 4 should use artifact-store `statvfs` available/free capacity. |
| `gpu_utilization_percent` | number or null | `0..100`, inclusive. Use null when no accelerator reading is available. |
| `gpu_memory_total_bytes` | integer or null | `0..17,592,186,044,416` (16 TiB), inclusive. Both GPU-memory fields are null or both are integers. The Task 4 GB10 unified-memory fallback may populate this pair from host total/available memory. |
| `gpu_memory_free_bytes` | integer or null | Same range; when present, `gpu_memory_free_bytes <= gpu_memory_total_bytes`. |
| `temperature_c` | number or null | `-100..300`, inclusive, degrees Celsius. |
| `power_watts` | number or null | `0..100,000`, inclusive. |
| `network_receive_bytes_per_second` | number or null | `0..1,000,000,000,000,000`, inclusive. Use null for a missing previous counter, counter reset, non-positive elapsed interval, or unavailable source. |
| `network_transmit_bytes_per_second` | number or null | `0..1,000,000,000,000,000`, inclusive, with the same null rules as receive rate. |
| `gap_samples` | integer | `0..9,223,372,036,854,775,807`, inclusive. This is the count of samples dropped before this retained sample because the bounded producer queue discarded oldest entries. |
| `details` | object | Optional; omission is equivalent to an object whose two fields are null. Only the fields in the next table are accepted. |

### `details`

| JSON field | Required | JSON type | Exact constraint |
|---|---:|---|---|
| `accelerator_name` | no | string or null | If non-null, 1 through 256 characters. |
| `accelerator_performance_state` | no | string or null | If non-null, 1 through 32 characters, for example `P0` or `P8`. |

The stored details JSON is additionally guarded by a 4,096-character SQL text bound. The exact schema above is substantially smaller than that defense-in-depth ceiling.

### Canonical example

```json
{
  "schema_version": 1,
  "samples": [
    {
      "boot_id": "00000000-0000-4000-8000-000000000001",
      "sequence": 42,
      "observed_at": "2026-08-15T00:00:00Z",
      "cpu_utilization_percent": 12.5,
      "load_average_1m": 1.25,
      "memory_total_bytes": 128000000000,
      "memory_available_bytes": 64000000000,
      "disk_total_bytes": 1000000000000,
      "disk_free_bytes": 750000000000,
      "gpu_utilization_percent": 25.0,
      "gpu_memory_total_bytes": 128000000000,
      "gpu_memory_free_bytes": 63000000000,
      "temperature_c": 41.5,
      "power_watts": 17.25,
      "network_receive_bytes_per_second": 1024.5,
      "network_transmit_bytes_per_second": 512.25,
      "gap_samples": 0,
      "details": {
        "accelerator_name": "NVIDIA GB10",
        "accelerator_performance_state": "P0"
      }
    }
  ]
}
```

## Public Python contract

`control/src/vonk_control/telemetry.py` exposes three frozen, slotted dataclasses:

- `TelemetryDetailsInput` contains only `accelerator_name: str | None` and `accelerator_performance_state: str | None`, with the wire lengths above.
- `TelemetrySampleInput` contains exactly the sample fields above, typed as `uuid.UUID`, `datetime`, strict Python integers, finite Python numbers, nullable metrics, and `TelemetryDetailsInput`. It deliberately has no node identity or receive time. Its static ranges are checked on construction; timezone/window validation is completed by `record_batch` before a transaction opens.
- `TelemetrySampleView` contains every normalized sample field plus controller-owned `id: str`, `node_id: str`, and `received_at: datetime`.

Repository methods:

- `TelemetryRepository.record_batch(node_id, samples)` accepts a sequence of 1 through 16 `TelemetrySampleInput` values and returns the corresponding persisted `TelemetrySampleView` tuple. Exact persisted replays are idempotent; a replay with changed content is rejected.
- `TelemetryRepository.latest(node_ids)` reads through `node_telemetry_latest` and returns a `dict[node_id, TelemetrySampleView]` without scanning history.
- `TelemetryRepository.history(node_id, start, end, maximum_points)` requires timezone-aware `start < end`, requires `maximum_points` in `1..1,500`, selects the newest bounded points, and returns them in chronological order.

## Persistence, transaction, and ordering decisions

- `node_telemetry_samples` is append-only history with a unique key on `(node_id, boot_id, sequence)`, typed nullable metric columns, separate non-null `observed_at` and `received_at`, bounded JSON details, and `ix_telemetry_node_observed(node_id, observed_at)`.
- `node_telemetry_latest` has one row per node and a unique foreign-key pointer to the current history sample. The pointer's sample deletion is restricted so maintenance cannot silently remove current latest state.
- Every batch is statically validated, normalized to UTC, checked against the five-minute/30-second window, checked for duplicates, and checked for in-batch ordering before `sessions.begin()`.
- The controller clock is read once per batch. That single value becomes every inserted row's `received_at`; no producer field can supply or overwrite it. Producer `observed_at` remains separate.
- One database transaction inserts all new history rows and advances the latest pointer. Any history insert, stored-head ordering check, or latest write failure rolls back the entire batch; the rollback test injects a latest-projection flush failure and observes zero history and zero latest rows.
- Within one boot, an unseen sample must advance both stored sequence and stored observation time. An exact older replay is a no-op. A conflicting replay or unseen regression is rejected.
- A different boot may reset sequence. Its history is retained, but it advances latest only when its `observed_at` is later than the current sample.

## Migration

- Alembic revision/head: `0023_node_telemetry`.
- `down_revision`: `0022_observation_latest_index`.
- Upgrade creates `node_telemetry_samples`, `ix_telemetry_node_observed`, and `node_telemetry_latest` with named checks/uniqueness/foreign keys.
- Downgrade removes latest, the history index, and history in dependency order.
- `ScriptDirectory.get_heads()` is asserted as exactly `['0023_node_telemetry']` in `test_admission_migration.py`.

## RED/GREEN evidence

Baseline and environment isolation:

| Phase | Command | Result |
|---|---|---|
| Baseline | `control/.venv/bin/pytest control/tests/test_agent_api.py control/tests/test_admission_migration.py control/tests/test_migrations.py -q` | `96 passed`, `1 failed`, 6 warnings. The sole failure was the pre-existing Darwin absence of `/proc/self/fd`. |
| Baseline reproduction | `control/.venv/bin/pytest control/tests/test_agent_api.py::test_invalid_ranges_do_not_leak_artifact_descriptors -q` | `1 failed`; `uname -s` was `Darwin` and `/proc/self/fd` was absent. No Task 3 code was involved. |

Strict Task 3 cycles:

| Phase | Command | Result |
|---|---|---|
| RED — repository | `control/.venv/bin/pytest control/tests/test_telemetry.py -q` | Collection error because `NodeTelemetryLatest`/telemetry production support did not exist. |
| GREEN attempt — repository | Same command | `25 passed`, `4 failed`; exposed SQLite timezone metadata loss and one public error-label mismatch. |
| GREEN — repository | Same command | `29 passed`. |
| RED — migration head | `control/.venv/bin/pytest control/tests/test_admission_migration.py::test_admission_state_is_linear_head -q` | `1 failed`; observed head `0023_node_telemetry` differed from stale asserted `0022_observation_latest_index`. |
| GREEN — migration | `control/.venv/bin/pytest control/tests/test_admission_migration.py control/tests/test_migrations.py -q` | `3 passed`. |
| RED — authenticated endpoint | `control/.venv/bin/pytest control/tests/test_agent_api.py -q -k telemetry` | `1 passed`, `9 failed`, 94 deselected; route/boundary returned `404`. The authentication-before-parse case already returned `401`. |
| GREEN attempt — endpoint | Same command | `5 passed`, `5 failed`; duplicate-key and body-boundary behavior passed, while a missing `uuid` import broke validated requests. |
| GREEN — endpoint | Same command | `10 passed`, 94 deselected. |
| RED — SQL half-pair defense | `control/.venv/bin/pytest control/tests/test_telemetry.py::test_sample_rejects_nil_boot_id control/tests/test_telemetry.py::test_database_rejects_half_present_capacity_pair -q` | `1 passed`, `1 failed`; SQL `CHECK` unknown semantics admitted a half-present capacity pair. |
| GREEN — SQL defense and repository | `control/.venv/bin/pytest control/tests/test_telemetry.py -q` | `31 passed`. |
| RED — bounded details | `control/.venv/bin/pytest control/tests/test_telemetry.py::test_details_are_exact_and_bounded -q` | `1 failed`; empty-but-present accelerator name was admitted. |
| GREEN — bounded details | Same command | `1 passed`. |
| RED — exact schema integer | `control/.venv/bin/pytest control/tests/test_agent_api.py::test_telemetry_schema_version_is_exact_integer -q` | `2 failed`; Pydantic coerced `true` and `1.0` to literal `1`. |
| GREEN — exact schema integer | Same command | `2 passed`. |
| RED — RFC 3339 wire type | `control/.venv/bin/pytest control/tests/test_agent_api.py::test_telemetry_observed_at_is_rfc3339_string -q` | `1 failed`; a numeric Unix timestamp was coerced and accepted. |
| GREEN — RFC 3339 wire type | Same command | `1 passed`. |
| RED — complete fixed metrics | `control/.venv/bin/pytest control/tests/test_agent_api.py::test_telemetry_requires_every_fixed_core_metric -q` | `1 failed`; omitted GPU utilization defaulted to unknown. |
| GREEN — complete fixed metrics | Same command | `1 passed`; fixed core fields are now required and nullable. |

Integrated/final verification:

| Command | Result |
|---|---|
| `control/.venv/bin/pytest control/tests/test_telemetry.py control/tests/test_agent_api.py -q -k 'not invalid_ranges_do_not_leak_artifact_descriptors'` | `134 passed`, 1 deselected. This preceded the final exact-wire additions. |
| `control/.venv/bin/pytest control/tests -q -k 'not invalid_ranges_do_not_leak_artifact_descriptors'` | `1,707 passed`, 56 skipped, 1 deselected, 68 failed, 42 errors. Failures were out of Task 3: missing Docker for mandatory PostgreSQL races, Darwin/Linux API differences, older stale migration-head assertions, and unrelated platform fixtures. |
| `control/.venv/bin/pytest control/tests/test_telemetry.py control/tests/test_agent_api.py control/tests/test_admission_migration.py control/tests/test_migrations.py -q -k 'not invalid_ranges_do_not_leak_artifact_descriptors'` | Final committed-tree evidence: `141 passed`, 1 deselected, 12 unrelated pytest temp-cleanup warnings. |
| `control/.venv/bin/python -m compileall -q control/src/vonk_control/telemetry.py control/src/vonk_control/models.py control/src/vonk_control/agent_api.py control/src/vonk_control/api.py control/migrations/versions/0023_node_telemetry.py` | Exit 0. |
| `git diff --check 174eb11..490df62` | Exit 0, no whitespace errors. |

## Self-review

- Requirements coverage: authenticated certificate identity is the sole node authority; actual body size is streamed and bounded; duplicate keys precede Pydantic; fixed wire fields and ranges are exact; observed and receive times are separate; validation precedes transactions; history/latest are atomic; `0023` is linear after `0022`; no prohibited file areas changed.
- Security review: the general request middleware bypasses telemetry body inspection, trusted-proxy middleware authenticates and injects typed scope identity, then the telemetry-specific request middleware reads/parses the body. Tests prove unauthenticated malformed JSON returns `401`, while authenticated duplicate/oversized bodies return `422`/`413`.
- Data-integrity review: unique sample identity, canonical non-nil boot UUID, finite/range checks, explicit SQL `IS NOT NULL` branches for capacity pairs, idempotent exact replay, latest pointer restriction, and rollback behavior are covered.
- Test-quality review: tests assert persisted rows, returned history/latest, response status, actual ASGI chunk reads, and real transaction rollback. No assertions target mocks, and no test-only method was added to production.
- Scope review: implementation commit contains exactly eight Task 3 control files. No Rust, frontend, recipe/runtime/readiness, live-system, push, or PR operation occurred.
- Process note: another workspace writer created and updated overlapping Task 3 files between the initial RED and GREEN patches and finalized the implementation commit. Work stopped for inspection, incompatible schema variants were reconciled, and the final committed tree was re-verified. The nil-UUID test passed on its first explicit run because compatible validation had appeared concurrently; the SQL half-pair behavior in that same cycle was RED before its fix.

## Concerns

1. This host cannot run the repository's mandatory Docker-backed PostgreSQL race suite, and Darwin lacks several Linux-only APIs used by unrelated tests. PostgreSQL migration round-trip is covered through Alembic's SQLite path, but a real PostgreSQL telemetry concurrency test remains desirable.
2. Same-node overlapping telemetry requests are protected against corruption by transactions and uniqueness, but the repository does not explicitly serialize first-writer/latest-pointer work with a PostgreSQL node-row lock. A rare overlap may surface as a retryable database conflict rather than a clean idempotent response; Task 4 should avoid concurrent sends per node, and a future PostgreSQL race test can justify adding serialization.
3. Several older migration tests still assert historical revisions as the sole Alembic head and therefore fail once any later migration exists. The requested admission linear-head assertion is updated; changing unrelated historical tests was intentionally out of scope.
4. Pytest repeatedly warns that macOS temporary worker directories are not empty during cleanup. This predates Task 3 and does not affect focused results.

## Fix round 1 — serialized ingestion and schema alignment

Implementation commit: `b63092d` (`fix: serialize node telemetry ingestion`), based on the Task 3 implementation at `490df62` and its report-only follow-up `7189a40`. This commit is local only; it was not pushed and no pull request was opened.

### Files

- `control/src/vonk_control/telemetry.py` — locks the authoritative `AgentNode` row with PostgreSQL `FOR UPDATE` in the ingestion transaction before reading the latest pointer, replay identity, or per-boot head. Exact concurrent replays therefore observe the committed row and remain idempotent; no generic `IntegrityError` handler was added.
- `control/src/vonk_control/models.py` — adds the `(node_id, id)` sample unique pair, a composite latest-pointer foreign key from `(node_id, sample_id)`, and database maxima matching the wire contract.
- `control/migrations/versions/0023_node_telemetry.py` — applies the same composite key and bounds while revision `0023_node_telemetry` is still new; `down_revision` remains exactly `0022_observation_latest_index`.
- `control/src/vonk_control/agent_api.py` — retains intentional Pydantic/domain `ValueError` behavior with a narrowly scoped Ruff exception.
- `control/tests/test_telemetry.py` — adds portable lock-order/SQL compilation, cross-node pointer, and database-bound regression coverage.
- `control/tests/test_telemetry_postgres.py` — adds deterministic concurrent identical-replay and delayed older-commit tests against disposable PostgreSQL.
- `control/tests/test_admission_migration.py` — verifies the composite FK/unique pair and exact persisted maxima.

### Task 4 wire contract

There are no wire field additions, removals, or renames in this fix. Task 4 must continue to emit the exact envelope, sample, and `details` field names and ranges in the tables above. In particular, all six capacity fields remain inclusive `0..17,592,186,044,416` bytes (16 TiB), and `network_receive_bytes_per_second` plus `network_transmit_bytes_per_second` remain inclusive `0..1,000,000,000,000,000` finite numbers. The database checks now match those limits exactly.

The exact allowed `details` keys remain enforced by the authenticated wire model and typed repository input. Cross-dialect SQL checks for JSON object shape/key sets were not added because portable JSON text/object semantics are not sound enough across SQLite and PostgreSQL; the database retains only its bounded serialized-size defense (`2..4,096` characters).

### Fix-round TDD and verification

| Phase | Command | Result |
|---|---|---|
| RED — lock and schema | `control/.venv/bin/pytest control/tests/test_telemetry.py -q` | `35 passed, 7 failed`; failures were exactly the missing node lock, cross-node latest-pointer acceptance, and five missing database maximum checks. |
| GREEN — portable repository/migration | `control/.venv/bin/pytest control/tests/test_telemetry.py control/tests/test_admission_migration.py control/tests/test_migrations.py -q` | `45 passed`; 12 pre-existing macOS temporary-directory cleanup warnings. |
| Authenticated telemetry API | `control/.venv/bin/pytest control/tests/test_agent_api.py -q -k telemetry` | `14 passed, 94 deselected`; 12 pre-existing temporary-directory cleanup warnings. |
| PostgreSQL race suite | `control/.venv/bin/pytest control/tests/test_telemetry_postgres.py -q` | `2 skipped`; Docker is unavailable on this laptop. Both tests were collected. |
| Ruff 0.16.1 | `uvx --from ruff==0.16.1 ruff check` over all Task 3 implementation/test files, including `test_telemetry_postgres.py` | `All checks passed!` |
| Compile | `control/.venv/bin/python -m compileall -q` over Task 3 Python implementation/migration files | Exit 0. |

### Fix-round concerns

1. The earlier concurrency concern is resolved in `b63092d`: all telemetry head and replay reads occur after the per-node authoritative row lock in the same transaction.
2. Docker is unavailable on this laptop, so the new real-PostgreSQL race tests could not execute locally. Portable coverage verifies that the lock is issued first and compiles to `FOR UPDATE OF agent_nodes`; CI or a Docker-capable host should execute the two integration cases.
3. Exact JSON `details` keys deliberately remain repository/wire enforced rather than duplicated in dialect-sensitive SQL checks, as described above.
4. The five stale migration-head tests identified by the controller remain unchanged and out of scope. The Task 3 migration tests pass.
5. The unrelated in-progress Task 5 plan edit in the shared working tree was not staged or committed.
