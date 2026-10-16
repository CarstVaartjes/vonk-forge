# Principle guards

`scripts/check-added-lines [PATCH_FILE|-]` checks added lines in an externally
supplied unified diff. CI creates the patch against its explicit PR/merge-group
base; local callers may supply their own patch. The script, Python scanners and
fixture tests consume source and patch text without reading Git or history.

New contract string literals, untyped mappings, provenance comparisons and
security refusals outside authentication, authorization and byte-verification
ingress owners fail. Existing wait, read, remedy, retention, test-principle and
coordination detectors also inspect added lines. Bounded waits remain valid.
No count ledger, baseline regeneration, category assignment or before/after
report is required. Untouched old findings cannot block a change.

One rule is whole-tree rather than added-line: a failure's retry follows its
one disposition (`vonk_agent_protocol.Disposition`, read through
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

A second whole-tree rule keeps an unknown effect from becoming a failure: a run
or rank is recorded `RunState.FAILED` only in a function that reads fresh
absence (`run_node_reports_absent`, `_proves_fresh_absence`, the absence
settlers, or the Spark report's `process_running` it is applying). A `failed`
run holds no claim, so an unconfirmed Stop or a Start nobody can stop exactly
is `lost` instead: stoppable, its claims held, observed until its Spark reports
it gone or a newer exact Stop ends it (`distributed_recovery.ended_run_state`).
`test_no_run_or_rank_is_failed_without_fresh_absence_anywhere` owns the rule.

These syntax checks do not prove recovery. Behavior tests must demonstrate
bounded observation and retry, reuse after a fault clears, and fresh admission
after an operation ends. `non_blocking.assert_ended_without_blocking` checks
release of explicitly inventoried holds and admission with a fresh request key.
`upgrade_continuity.assert_upgrade_keeps_serving` checks serving continuity with
real observations and an upgrade/restart callback in its owning lane.

The data-contract registry, pyright exceptions, serde/TypeScript shape registries
and generated-contract checks retain their existing role. Controller startup and
exclude-unset registries describe semantic exceptions rather than debt counts.
