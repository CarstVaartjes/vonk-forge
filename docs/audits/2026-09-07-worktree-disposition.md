# Remaining worktree disposition

> Historical/as-of status: This is a historical repository/progress/evidence snapshot, valid only as of its stated baseline/date. Do not read it as current deployment state.

Reviewed against main `c34ef50a` after PR622 merged.

| Work area | Added value and disposition |
| --- | --- |
| Original `/opt/vonk-forge` checkout | Installer channel rendering, publication, acceptance and compact Library behavior are already present or superseded on main. Six untracked design documents match main; three have older progress or design variants. Preserve the useful greenfield/central-contract clarification in the current recipe design notes and restore the implementation-review links. Discard superseded working changes and use this path for clean main. |
| Supply-chain review | Three modified SBOM outputs describe an older source/dependency state. Current main has regenerated outputs and passing verification. Discard these generated copies. |
| Legacy catalog retirement | Eleven staged paths match main, including removed harness configuration. Remaining variants retain retired catalog/registry dependencies that current main removed. No missing production behavior was found. Discard the intermediate variants. |
| Temporary integration checkout | All useful commits are merged through PR622 and the documentation follow-up. Remove after moving main back to the primary checkout. |

Before cleanup, retain small recovery snapshots of each dirty checkout's HEAD,
status, binary working/staged patches and the nine untracked design documents.
The original `.tmp-direct-test` tree contains disposable pytest databases and
synthetic runtime-image fixtures, not NAS/Spark data. Remove it with the stale
Finder metadata. This cleanup does not touch live databases, caches or workloads.

No old branch is merged merely to preserve its ancestry: differences are judged
against the final implementation. Git history and the recovery snapshots preserve
the discarded intermediate work without keeping another active checkout.
