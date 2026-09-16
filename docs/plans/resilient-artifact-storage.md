# Approach to resilient Controller integrations

Status: approved direction. Both phases are implemented. The coordination
boundaries are machine-checked by
`control/tests/coordination_boundaries.py` and its shrink-only baseline
`tools/coordination-baseline.json`, which is now empty: the four audited
SQL-over-storage edges are closed, and every audited artifact lock is either a
bounded nonblocking claim with a contention test or a descriptor the scanner
proves no other caller can contend for. Phase 2 (artifact availability and local
checkpoints moving from SQL to managed storage) moved model object availability
and the runtime-image receipt to managed storage and verified that the
`recipe_builds` half needs no cutover. This is repository and CI evidence;
deployed Controller recovery and physical Spark acceptance remain separate,
unexercised boundaries.

This is a high-level approach, not a task breakdown. The
[architecture overview](../architecture-overview.md) owns the strict state,
coordination, and recovery boundaries. Existing cache-restore and workload
recovery are the starting point. A documentation change does not establish
runtime conformance or deployed acceptance.

## Intended outcome

Every part of Vonk Forge works toward the latest authorized desired state.
Temporary failure, restart, delayed evidence, or a stale local view must not
lose accepted intent, discard useful completed work, or permanently block a
fresh authorized request. Once dependencies recover, reconciliation continues
without manual editing of database rows or status files.

Eventual consistency applies to observed progress, artifact availability,
execution, and derived views catching up with accepted intent. Permissions,
revocations, ownership, exact plan identity, and publication admission remain
strictly checked. A stale observation never authorizes an action.

## 1. Establish one owner for every fact

Keep PostgreSQL for transactional control decisions: identities, permissions,
catalog approval, saved profiles, desired state, request identity, job ownership,
cancellation, reservations, route decisions, audit, and retained telemetry.
Managed storage owns artifact bytes, verification manifests, and local
transfer/build checkpoints. Spark agents own their execution journals and
report authenticated evidence of effects. Routes and UI summaries are derived
from these owners.

Make each integration explicit about what it owns, what it observes, and which
owner resolves a disagreement. Remove duplicate administration. An index or
cached view must be reconstructible without inventing authority. The existing
PostgreSQL service, including LiteLLM's separate database, remains in scope as
infrastructure; replacing it is not part of this direction.

## 2. Make every handoff recoverable

Connect accepted intent, preparation, distribution, runtime effects, and route
publication through exact identities and durable acknowledgements. A retry
continues the same request; a new explicit request has a new identity and can
supersede older intent. Receivers recognize replay and inspect existing effects
before repeating work. Completion is acknowledged only after its evidence is
durable; lost acknowledgements can be recovered without repeating the effect.

Use reconciliation at each handoff instead of assuming that a database commit,
filesystem publication, agent action, and route activation happen atomically.
Each part must be able to discover what completed, what remains, and whether
its intent is still current after interruption. No component may silently
substitute a different model, image, plan, or authorization during recovery.

## 3. Prevent circular waits and contain failures

Enforce the architecture's common lock order, short transactions, nonblocking
claims, parent/child ownership rules, and bounded resource acquisition before
changing persistence ownership. Waiting work releases resources needed by its
dependencies. Every wait has a visible cause, an owner, a resume condition, and
a deadline that survives restart. Timeouts supplement these boundaries; they
cannot replace a design that prevents cycles.

`control/tests/coordination_boundaries.py` now enforces the two provable rules:
a SQL transaction never spans external work, and an artifact lock is acquired
outside a transaction, nonblockingly, and one at a time. CI runs it, and
`tools/coordination-baseline.json` records accepted exceptions with a written
reason under a gate that fails on both a new site and a stale entry. The
baseline is now empty: every audited site is either converted to a bounded
nonblocking claim with a contention test, or proven in the syntax tree to hold
a descriptor no other caller can contend for.

Keep failures local to the affected request, object, source, or target.
Independent eligible work continues. Recovery uses bounded concurrency and
backoff, preserving partial transfers and verified results. Invalid contracts,
revoked authority, denied access, and integrity failures remain actionable
conditions; correcting them permits authorized recovery without weakening the
checks. Cancellation and newer intent survive delayed results and takeover.

## 4. Converge through the normal operating path

Use the same preparation and execution paths for initial work, retry, and
recovery. Reuse valid artifacts, resume compatible partial work, and safely
replace damaged temporary work. Failed refreshes and rebuilds preserve the
last verified result. Exact changed build outputs require a new accepted
binding rather than silently changing a running or already bound plan.

Reconcile API/worker intent, local storage, Spark results, and route publication
independently but under their shared request and attempt identities. Progress
views observe this work without blocking it. Cleanup coordinates with current
references and active work; missing evidence causes a defer, never inferred
permission to delete. A restore preserves surviving data and re-establishes
control authority before adopting artifacts or reclaiming them.

## 5. Change ownership in complete, bounded steps

Start by auditing cross-component waits and enforcing the coordination
boundaries. Then move artifact availability and local checkpoints from SQL to
managed storage, first for models and then images/builds, connecting their
admission, observation, cancellation, cleanup, and restore behavior as each
boundary changes.

Each step must leave one working execution path and one owner for its facts.
Update producers and consumers together and remove the obsolete persistence
responsibility. Preserve current security and recovery guarantees, useful
cached assets, and exact identities. Do not add dual readers/writers, legacy
schema compatibility, another scheduler, or new infrastructure to bridge the
change. Arbitrary jobs, hooks, and package upgrades do not acquire automatic
replay merely because artifact preparation supports recovery.

The bounded cutover is:

1. **Model object availability.** Implemented. An object's verification receipt
   moved to managed storage beside the bytes it describes. SQL keeps the logical
   `model_cache_sets` membership that binds a profile to an exact artifact-set
   digest, and the `model_cache_artifacts` table is deleted with its model,
   schema entry, and test fixtures. An object whose receipt is absent is
   unavailable for admission even when bytes are present, and an object whose
   receipt is present but whose bytes are gone is reported as a repair
   blocker rather than admitted. The audit found twelve `ModelCacheArtifact`
   consumers that move together: `_ensure_set` row creation, the download and
   verify progress checkpoint, `_mark_artifact_verified`, `_verified_bytes`,
   `_managed_cached_objects`, the artifact resolution path,
   `reconcile_storage`, `storage_summary`, and the two removal paths in
   `_remove_model_content`.
2. **Image and build checkpoints.** Implemented. The prepared runtime-image
   receipt is now the storage fact and SQL keeps only the authorization that a
   fence conditionally accepts. `RuntimeImageReceipt` and its table, model,
   constraints, and foreign key are deleted; `RuntimeImageAuthorization` is
   keyed by its own `(recipe_revision_id, effective_execution_key,
   oci_archive_sha256)`. Availability requires both a live matching
   authorization and the stored receipt, so neither a deleted authorization
   with bytes on disk nor a live authorization with the receipt removed is
   admitted.

   **The `recipe_builds` half needs no cutover, and this was verified rather
   than assumed.** `RecipeBuild` carries the build request identity (recipe
   revision, builder node, source bundle, build input, state, plan, policy
   report) and the builder's reported output identity (`image_digest`,
   `oci_layout_sha256`, `image_bytes`). It stores no transfer or build
   checkpoint: there is no byte counter, no partial offset, and no upload
   progress in the row. The archive, its partial staging, and the immutable
   receipt already live in managed storage, and `_authorize_current_revision`
   checks the row's output identity against that stored receipt before any
   authorization is written. The row is therefore SQL's record of which builder
   produced which exact archive, not a second availability copy, which is the
   ownership split the architecture asks for.

   The reconnaissance that preceded the image work remains as context:

   * The SQL row model (`models.RuntimeImageReceipt`, imported elsewhere as
     `RuntimeImageReceiptRow`) has 147 references across thirteen modules
     (`runtime_image_preparation` 56, `distribution_executor` 32,
     `distribution` 19, `run_switch_operations` 13, `recipe_image_availability`
     9, `model_cache` 8, `execution_plan_service` 5, `api` 4, `agent_api` 4,
     `run_switch_contract` 2, `library_projection` 2,
     `availability_production` 2, `models` 1) plus 35 in `control/tests`.
     The storage contract `RuntimeImageReceipt` is a different type with the
     same name, so a naive rename is not the change.
   * `persist_runtime_image_receipt` states that "filesystem receipts are an
     object cache" and "the SQL row is the authority consumed by install
     admission and agent specification reads". Phase 2 reverses that sentence,
     so the cutover has to move admission authority deliberately rather than
     delete an unused copy.
   * The row supplies a join key (`RuntimeImageAuthorization.receipt_id`), a
     duplicated availability flag (`state == "verified"`), and the
     `verified`→`evicted` transition used by `remove_selector`. The
     authorization table already carries every identity field it compares, so
     the natural key is its existing `oci_archive_sha256`; the receipt row is
     what makes the mapping indirect.
   * Revocation must not be dropped. The authorization `state` already has
     `authorized`/`revoked`; the receipt `state` is the local-availability flag
     that becomes storage-owned.

   The bounded cutover is: delete the receipt table and row model; re-key
   `RuntimeImageAuthorization` by its own `oci_archive_sha256`; read
   availability and the archive from the storage receipt; keep SQL for the
   authorization decision, the revocation, and the exact reference a fence
   accepts. It lands as its own package off the current main, with the
   `control/tests/test_runtime_image_preparation.py`,
   `test_recipe_image_availability.py`, `test_direct_run_switch_production_path.py`,
   `test_agent_api.py`, and `test_availability_production.py` consumers updated
   in the same commit.

## Disposition of the audited lock sites

The baseline holds no sites. The audit is worth recording because the first
reading -- "flip them all to `LOCK_NB`" -- was wrong for almost every one of
them. Each site is a **cross-process serialization lock on one named resource**,
not an artifact lock waiting on another artifact lock, and each ended in one of
two states.

Converted to a bounded nonblocking claim, each with a contention test that holds
the lock from a separate process:

* `route_runtime._locked` serialized route publication with an unbounded
  blocking acquisition. It now claims the lock with `LOCK_NB` inside a bounded
  budget and reports contention to its caller, which retries the whole
  publication through normal reconciliation.
* `runtime_image_preparation.pull_and_export` serializes writers of one OCI
  index across worker processes. The claim is now `LOCK_NB` inside a bounded
  budget, and contention raises a retryable
  `runtime_image.transfer_contended` failure so the operation is rescheduled
  instead of parking a preparation slot across a network transfer. This is the
  one site where removing the wait does not weaken a fence, because the caller
  already replays the whole preparation.
* `artifact_blob_store._reference_lock` is taken by `reference_attachment()`
  (shared) and `reference_reconciliation()` (exclusive), and the attachment form
  is deliberately held across a blob verification *and* its durable database
  attachment so a reconciler cannot reclaim a blob between the two. The fence
  still spans both steps; only the acquisition changed. It now claims inside a
  bounded budget and reports contention as an `ArtifactBlobStoreError`, which
  the upload route returns as a 409 conflict so the caller abandons and replays
  the upload, and which a scheduled reconciliation pass surfaces to its caller
  rather than parking a worker on another process' reconciliation. That
  caller-side replay is what makes the nonblocking shape safe here, which is why
  this site needed a protocol answer rather than a flag change.

Proven in the syntax tree to be uncontended, so the scanner accepts them:

* `artifact_blob_store._reserve` blocks on a reservation file lock. The
  descriptor comes from an `O_EXCL` create, so it names a file only this call
  can hold and the lock can never contend. The scanner proves that from the
  syntax tree instead of reporting every blocking `flock`, with tests for the
  private descriptor, the shared descriptor, and the nonblocking shared
  descriptor. Quota arithmetic is unchanged.
* `recipe_image_availability._run` holds an in-process identity guard while
  taking the removal lock; the scanner tells such a guard from an artifact lock,
  because the two are related guards rather than two artifact locks.

What the coordination rules require of these is that no such wait sits inside a
SQL transaction or waits on a child, and the scanner enforces both.

## Where the lock guarantees are proven

The three nonblocking claims are proven in the **lane tier**, in
`control/tests/test_os_lock_contention.py`, because `flock` semantics are the
guarantee: the holder runs as a **separate process** (a second descriptor in the
same process would succeed, since `flock` is per open file description), the
claim must return inside its budget, and the release must restore availability.
A mocked acquisition would establish nothing. Run them with `-m lane` in
OrbStack or the designated Linux lane; they pass there in about a second. The
complete control suite in CI does not filter the marker, so the `control-suite`
shards also run them on `ubuntu-24.04`; `-m "not lane"` describes the local fast
tier, not a CI omission.

The static rules stay in the fast tier, in
`control/tests/test_coordination_boundaries.py`, because they are pure syntax
tree analysis.

## Evidence of success

Validate complete interactions under contention, process death, lost responses,
restarts, cancellation, storage loss, and dependency recovery. Demonstrate that
no allowed wait cycle remains, stale attempts cannot regain authority,
completed work survives, unrelated work advances, and the system eventually
converges after recoverable faults clear. Include the real PostgreSQL, OS-lock,
worker, agent, and publication boundaries wherever those guarantees depend on
them; mocked local success cannot establish integration resilience.

Record repository/CI evidence separately from deployed Controller recovery and
physical Spark acceptance. Update operator guidance when behavior ships. This
approach requires observable recovery and bounded waiting, rather than a
blanket promise that untested code can never deadlock.
