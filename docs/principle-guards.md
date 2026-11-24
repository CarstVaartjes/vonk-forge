# Principle guards

`scripts/check-added-lines [PATCH_FILE|-]` checks added lines in an externally
supplied unified diff. CI creates the patch against its explicit PR/merge-group
base; local callers may supply their own patch. The script, Python scanners and
fixture tests consume source and patch text without reading Git or history.

New contract string literals, untyped mappings, provenance comparisons and
security refusals outside authentication, authorization and byte-verification
ingress owners fail. Existing wait, read, retention, test-principle and
coordination detectors also inspect added lines. Bounded waits remain valid.
The `plan-validation` guard refuses an added `.validate()` on a compiled
plan (also the rustfmt split-line shape) in `rust/crates/vonk-agent/src` and
`rust/crates/vonk-agent-protocol/src/compiled_oci/` outside
`executor/plans.rs` (`refuse_invalid_plan` at dispatch; its
`parse_compiled_execution_plan` is the probe/test parser). `oci/custody.rs` and
`oci/observation.rs` re-validate persisted copies read back from disk; removing
those is a known follow-up. A plan is validated once where it enters.
No count ledger, baseline regeneration, category assignment or before/after
report is required. Untouched old findings cannot block a change.
A handler header respelled between `except (A, B):` and PEP 758's
`except A, B:` (the Python 3.14 formatter's spelling) is one handler: the
respelled line spends the deleted one and is not an added line.

The published index's kind (`recipe-library-index`) and package media type
(`application/vnd.vonk-forge.recipe-package.*`) are owned by `vonk_forge_contracts`:
spelling either as a literal on an added Python line fails
(`control/tests/contract_constant_copies.py`); read the index with
`vonk_forge_contracts.catalog_reader.read_catalog` and import the constants.

A stored Recipe, Model or display document has one reader each: the tolerant
`read_recipe`, `read_model` and `read_*_display` of `vonk_forge_contracts`. An
added line that indexes such a raw document, or validates one strictly with
`ModelDefinition`/`RecipeDefinition` `.model_validate*` or a `TypeAdapter` of
them, fails (added-line mode `document-reads`,
`control/tests/document_read_boundaries.py`): the strict contract refuses a
field that a row of an older contract still carries, so every start of such a
revision would be refused.

The contact-clock detector rejects new negative-age liveness refusals for
Controller-stamped contacts and worker heartbeats, including local aliases and
SQL freshness filters. Clock rollback and capture latency imply age zero;
certificate validity and agent-stamped observation freshness remain separate.

A test may not put a short wall-clock limit (5 s or less) on real work:
`future.result(timeout=1)`, `asyncio.wait_for(work, timeout=1)`, an asserted
`event.wait(2)`, a `communicate`/`subprocess` timeout or a
`time.monotonic() + 2` deadline fails at random on a loaded runner and proves
nothing pytest-timeout does not. Bound a hang guard generously (30 s) and prove
"does not wait" by behaviour: a lock-wait probe, a fake clock or event order.
A wait the test expects to expire (`not done.wait(0.1)`) and a polling interval
in a loop are not limits on work. A deliberate short limit names its reason on
the call: `# wall-clock: <reason>` (`control/tests/short_wall_clocks.py`).

A control test holds threads together with
`insert_collision_support.Rendezvous`, never a raw `threading.Barrier`
(Ruff `TID251` banned-api; `insert_collision_support.py` is the one exempt
file). A barrier in a patched product method waits
again when the product re-enters that method on the same thread (a replay after
a unique-key conflict, a replan), and the test then breaks the barrier at random
instead of naming the re-entry. Each thread passes a rendezvous once; a second
wait fails at once. `collide_inserts` holds two real admissions at their INSERT
so the unique key, not commit timing, decides the race.

A handler that catches a broad `Exception`, `ValueError`, `RuntimeError` or
`KeyError` and parks or defers work (`_decline_retry*`, `_defer_*`,
`_park_for_retry`) must classify the error with `disposition(` or
`is_definite(` in the same handler (added-line mode `retry-dispositions`). An
internal `InvalidValue` is a `ValueError`: parked as a conflict, it repeats on
every retry with nothing changed (#2299). A deterministic cause ends the work
with its evidence; a cause that clears by itself keeps waiting. An edit inside
an older handler of that shape is flagged too.

Retention under `unused_storage_collection/` and
`model_cache/fleet_removal.py`, the update notice (`recipe_update_notice.py`)
and the measured-peak carry (`observed_run_memory.py`) may not compare a recipe revision's
`content_digest` with the head's (anything named `head` or `newest`) to decide
"is this what the head runs" (added-line mode `head-digests`,
`control/tests/retention_head_digests.py`). A release that changes only
metadata gives the head a new digest and the same execution key, so that
comparison uninstalled the loaded profile's idle installation 24 hours after
every such release. Use the head revision itself or
`content_identity.runs_identically`.

A cached model set's `ModelCacheSet.model_content_sha256` is the Model that
first requested it, not every Model it serves. A new comparison of that column
with a Model digest (`==`, `!=`, `.in_`) on an added line outside
`model_cache/lineage.py` is flagged (added-line mode
`set-first-requester`, `control/tests/model_set_first_requester.py`). A release
that re-digests a Model of the same files keeps the old first requester, so
such a reader missed every set the new digest uses: eviction offered sets a
profile needs and miscounted shared Spark files. Read the set's bindings
through `model_cache.lineage` (`model_sets`, `verifies_model`,
`stored_set_bindings`) or, for what an installation runs,
`RecipeInstallation.model_artifact_set_sha256`.
The same mode flags a query selecting only
`RecipeInstallation.model_content_sha256` joined to `InstallationNode`: "which
Models does this Spark hold" by the Model each installation was made for. After
the contracts 5.0 re-digest it judged a Spark holding the files under the old
digest empty, so the load review charged the whole model again. Ask by the set
instead (`disk_reservations.model_sets_stored_on_node`).

NAS coverage is required only where NAS bytes are read. A new call of
`_require_managed_cache_coverage`, `resolve_verified_artifact_set`,
`objects_for_set` or `verified_model_objects_for_set` on an added
line outside `model_cache/` and `distribution/model_cache.py` is flagged
(added-line mode `model-sets`). A planner or compiler that required it
downloaded a model every target Spark already held, after the NAS evicted it
(#2337), and a grant built from the NAS set parked a copy that sends only the
image. Compile model receipts with `distribution.verified_model_receipts`;
build a grant's objects with `distribution.types.plan_objects`.

Three rules are whole-tree rather than added-line. The first: a failure's retry follows its
one disposition (`vonk_agent_protocol.agent_words.Disposition`, read through
`failure_classification.disposition`). A `WAIT` never spends a retry and never
ends work, a `FAIL` ends on its first occurrence with its cause as evidence,
and only an `OBSERVE` (an effect nobody could confirm) spends the retry count.
A spent count stops the spending, never the work: an irreversible effect still
unknown is lost (kept, its claims held, observed by its identity at the bounded
backoff's ceiling) until it is observed, reported or stopped by an exact Stop; a cancel or
newer intent still completes. An adapter whose kind dispatches to a Spark reads
the stored observation of its exact attempt in `observe`, never a constant:
`test_no_spark_dispatching_adapter_observes_a_constant` (whole tree, modules
that import `AgentOperationAttempt`) and the behaviour test
`test_spark_order_observe_reads_its_exact_attempt` own that.
`test_failure_disposition` fails any Controller or CLI site that builds a
failed lifecycle report outside `Reported.failed`, sets a retry flag by hand,
keeps a private `RecoveryPolicy` or failure limit, counts its own `*_attempts`,
or lets a `*Busy`/`*NotReady` type decide whether a retry is spent.

The second keeps an unknown effect from becoming a failure: a run
or rank is recorded `RunState.FAILED` only in a function that reads fresh
absence (`run_node_reports_absent`, `_proves_fresh_absence`, the absence
settlers, or the Spark report's `process_running` it is applying). A `failed`
run holds no claim, so an unconfirmed Stop or a Start nobody can stop exactly
is `lost` instead: stoppable, its claims held, observed until its Spark reports
it gone or a newer exact Stop ends it (`distributed_recovery.ended_run_state`).
`test_no_run_or_rank_is_failed_without_fresh_absence_anywhere` owns the rule.

The third: a model download or repair operation is created only by
`admit_transfer` (`tests/test_transfer_admission_guard.py`). It takes the
model-set gate and attaches to a live transfer where the rule in
[model-cache.md](runbooks/model-cache.md) says so; any source under `control/src` that calls `new_operation(` outside
the helper must be in the test's reasoned exemption list (a removal, the adapter
constructors, run-switch job rows).

Lock and ordering guards:

- `control/tests/intent_ordinal_writers.py` (added-line mode `intent-ordinal`)
  refuses numbering a workload intent, or writing `workload_intent_ordinal`,
  outside `workload_intent.py`; use `allocate_workload_intent` or
  `take_workload_intent`.
- `control/tests/durable_revocations.py` (added-line mode `durable-revocations`)
  refuses an added write of `revoked_at`, `ca_revoked_at`, `disabled_at` or
  `password_verifier` in `control/src` outside `enrollment/` unless the function
  calls `require_durable_commit` (Controller connections commit asynchronously;
  enrollment gets durability from `EnrollmentCore._transaction`). Internal
  revocations nobody is told about (`attempt_residues.py`, `local_ca.py`) are
  out of scope and stay asynchronous.
- `control/tests/test_agent_node_lock_tree.py` proves telemetry, inventory,
  contact, `installed_bytes.py` and `agent_api` modules never lock `agent_nodes` (the `optimistic`
  scan, `scan_agent_node_locks`).
- `fixed_tick_polls` (added-line mode `tick-polls`, in
  `control/tests/added_line_guards/`) refuses a test that calls
  `.tick()` in a fixed-count loop to poll a threaded model-cache outcome, or
  asserts that outcome after such a loop. Wait with `_drain`, `_drain_until` or
  `_wait_for`.
- The target-budget harness (`control/tests/test_target_budget.py`) builds both
  worlds under `ordered_ids`: ids count down, so the target sorts after its
  history and rows a check adds sort before it. The SQLite step count is
  deterministic and must be equal per statement, with no allowance.

The shared vector `agent_protocol/fixtures/log-tail-omission-v1.json` pins, for
the Rust agent and the Controller, the omission marker a retained log tail
carries between its cause lines and its window, and how a reader bounds such a
tail (`clamp_retained`, `_keep_retained`).

These syntax checks do not prove recovery. Behavior tests must demonstrate
bounded observation and retry, reuse after a fault clears, and fresh admission
after an operation ends. `non_blocking.assert_ended_without_blocking` checks
release of explicitly inventoried holds and admission with a fresh request key.
The `ending-without-fresh-request` guard names an ending by call name but
ignores request and payload builders (`*_request`, `*_payload`, `*_payloads`),
which end nothing; a name with an ending word never counts as the fresh request.
`upgrade_continuity.assert_upgrade_keeps_serving` checks serving continuity with
real observations and an upgrade/restart callback in its owning lane.

The data-contract registry, serde/TypeScript shape registries
and generated-contract checks retain their existing role. Controller startup and
exclude-unset registries describe semantic exceptions rather than debt counts.

A shared id type is defined once. The `shared-id-patterns` added-line guard refuses a new string that equals the digest, OCI digest, node id or UUID pattern outside the two `identifiers.py` files and `route_activation.py` (loaded with no package around it); import the constant or the `Annotated` type (`control/tests/shared_id_patterns.py`).

## Stored rows are not re-hashed

A row records its digest when it is written; comparing a stored `*_digest` /
`*_sha256` column with a fresh digest of the same row's own payload, plan or
document (added-line mode `identity`, "stored row re-hashed") refuses the row
whenever a newer Controller reads it back differently. Digests are verified at
ingress only (`REHASH_INGRESS`). The guard also catches a `payload_digest`
column compared with any `*_digest(...)` call, not only `sha256`.
