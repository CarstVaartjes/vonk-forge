# Cache progress integration for issues 593 and 594

> Historical/as-of status: This is a historical repository/progress/evidence snapshot, valid only as of its stated baseline/date. Do not read it as current deployment state.

Based on integrated platform `90ae6140368c20efbdb25385f0778e79198bb9ad`.
No live NAS or Spark was touched.

The cache now persists a required nested `OperationProgress` measurement. Its
bytes/items agree with the dedicated cache counters, and it carries the shared
receipt timestamps, elapsed time, rolling throughput, ETA and member fields.
The former duplicate top-level rate/ETA/member fields are removed. Current
producers, consumers and fixtures use this single structure without old readers.

`model_cache_progress.py` samples through the shared `observe_progress` and ages
snapshots through `project_progress`. Adjacent durable receipts determine rates;
a 45-second observation gap starts a fresh rate window. Unknown sizes have no
ETA. Hash verification is published before hashing and has no transfer estimate.
Normal byte checkpoints write at most once per second per operation. Phase and
terminal/error boundaries force the final counter to persist. Retry snapshots
start queued without carrying a prior transfer estimate.

The shared Activity provider, dedicated cache API, Library download/cache views,
CLI formatter, Run/Switch model-download receipt and Model/image availability
aggregation consume the canonical measurement. Per-object rates are retained,
while aggregate transferred bytes count shared objects only once. Models with
more than 1024 unique objects use exact aggregate totals without publishing an
incomplete per-object list. Progress therefore imposes no model shard limit.
The current model-cache progress database bound is 1 MiB, matching its Pydantic
UTF-8 bound; the fresh-schema definition in migration 0015 is updated as well.

## Integration notes

Regenerate OpenAPI and TypeScript/Python clients. `ModelCacheOperationProgress`
now requires `measurement: OperationProgress`; removed fields are
`bytes_per_second`, `eta_seconds`, and `members` at its top level.

The narrow `RunSwitchChildProgress.operation` field and `OperationProgress`
import duplicate the coordinated issue-597 worker change exactly. Keep one
copy when merging. That worker owns propagation from the phase receipt into
Run/Switch progress; this branch owns `_cache_result` only.

## Verification

- Combined cache, concurrency/recovery, Hugging Face authorization, distribution,
  Model/image availability and CLI tests: 207 passed, 2 skipped because their
  explicit PostgreSQL test inputs were not configured for that invocation.
- Three new checks cover actual persisted one-second checkpoints, service restart
  retaining adjacent receipt rates and smoothing, forced hashing counters,
  unknown totals, stale projections and a 1025-object aggregate.
- Library component tests: 9 passed, including displaying a rate supplied only
  in the canonical nested measurement.
- Ruff 0.16.1 and whitespace checks pass. Full generated-client/type checking is
  an integration check after root regenerates the shared artifacts.
