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
`test_failure_disposition` fails any Controller or CLI site that builds a
failed lifecycle report outside `Reported.failed`, sets a retry flag by hand,
keeps a private `RecoveryPolicy` or failure limit, counts its own `*_attempts`,
or lets a `*Busy`/`*NotReady` type decide whether a retry is spent.

These syntax checks do not prove recovery. Behavior tests must demonstrate
bounded observation and retry, reuse after a fault clears, and fresh admission
after an operation ends. `non_blocking.assert_ended_without_blocking` checks
release of explicitly inventoried holds and admission with a fresh request key.
`upgrade_continuity.assert_upgrade_keeps_serving` checks serving continuity with
real observations and an upgrade/restart callback in its owning lane.

The data-contract registry, pyright exceptions, serde/TypeScript shape registries
and generated-contract checks retain their existing role. Controller startup and
exclude-unset registries describe semantic exceptions rather than debt counts.
