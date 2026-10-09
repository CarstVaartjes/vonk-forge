# Principle guards

`scripts/check-added-lines [BASE]` checks only lines added since the merge base
with `origin/main`, or the explicit PR/merge-group base supplied by CI. Git is
confined to this workflow adapter. The Python scanners consume source and patch
text, and their tests use fixtures without Git or history.

New contract string literals, untyped mappings, provenance comparisons and
security refusals outside authentication, authorization and byte-verification
ingress owners fail. Existing wait, read, remedy, retention, test-principle and
coordination detectors also inspect added lines. Bounded waits remain valid.
No count ledger, baseline regeneration, category assignment or before/after
report is required. Untouched old findings cannot block a change.

These syntax checks do not prove recovery. Behavior tests must demonstrate
bounded observation and retry, reuse after a fault clears, and fresh admission
after an operation ends. `non_blocking.assert_ended_without_blocking` checks
release of explicitly inventoried holds and admission with a fresh request key.
`upgrade_continuity.assert_upgrade_keeps_serving` checks serving continuity with
real observations and an upgrade/restart callback in its owning lane.

The data-contract registry, pyright exceptions, serde/TypeScript shape registries
and generated-contract checks retain their existing role. Controller startup and
exclude-unset registries describe semantic exceptions rather than debt counts.
