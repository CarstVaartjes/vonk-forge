# Agreed plan: progress review

Review date: 2026-09-05. Platform integration inspected at 329c29c7, including latest origin/main 852fd17e. This is an implementation progress report, not release/deployment completion. Authoritative detailed acceptance ledger lives in the integration worktree; it is being refreshed to distinguish implemented behavior from missing validation.

| Agreed scope | Done in repository/integration | Remaining |
|---|---|---|
| Simple interface | Compact top navigation; linked Models/Recipes/Cache/Profiles; normal Download/Run/Switch without mandatory preparation/review; model identity deduplication; Fleet roster prioritized; profiles normal saved view | Correct rich-history chart identity/offline handling and final visual confirmation; connected deployed journeys |
| Models and recipes | Separate typed model identity/capability and recipe support projections; family/version/variant overview | Capability facts are shown only with evidence; inventory coverage needs explicit final ledger, unknown values must not imply completed capability research |
| Existing recipe conversion | 84/84 recipes packaged; 92 model versions; 250 embedded entities, 258 with harnesses; latest-main inventory reports no conversion gaps; the current 4b0e6a7 fixture hashes and sizes match its index | CI/merge/publication in reader-first order |
| Incremental recipe downloads | Persistent digest cache, self-contained package import, changed-only fetch, offline single-recipe import, atomic generation/failure continuity; real 84-package HTTP fixture passes | Hosted/deployed publication+sync verification |
| NAS model/container cache | Exact metadata/model/aux/image identities, real queued downloads, repair/reference protection, durable actual-byte progress | Deployed storage/retention and upstream source journey |
| Spark redistribution | Production cache-to-two-target service composition, authenticated manifest/range stream, verification/import evidence and resume; Rust default-stack crash fixed and native HTTP/range consumer test passes on ARM64 Linux; the production protocol wheel, lock and SBOM now include the operation | Actual OCI import and physical Spark journey remain separate |
| Automatic Run and profiles | Build/cache/copy/start orchestration; full profile scope and idle targets; preserve desired healthy runs and retained caches; service A→B→A tests | Final merged-protocol connected checks, deployed full switch/reuse/health validation |
| Progress/recovery | Durable family/global Activity, stable request keys, restart/resume, nonzero per-target projection tests (59 passed) | Final browser evolving-progress evidence and cross-client deployed parity |
| Metrics | Native/rich telemetry contracts, history persistence, typed clients, detail groups, units/provenance/availability | Correct actual per-series chart mapping and offline gaps; full adapter/source coverage review and physical sensor validation |
| Runtime writable paths | Central engine/harness-owned paths; vLLM and supported engine audit; reserved recipe variables rejected; MIA detection corrected; non-root/read-only-root OrbStack probe | Final merged-main checks and deployed runtime verification |
| CLI | Matching Download/Run/Switch, profiles, cache, telemetry/history and durable progress with JSON; integrated command/client tests pass | Final schema drift and deployed parity |
| Delivery | Scoped integration commits and cross-repo conversion exist; GitHub auth verified; latest main merged; release ordering agreed | PR/CI/merge/release/deployment not completed; no claimed live NAS/Spark upgrade |

## Verification already recorded

- The frozen Controller suite at 571f30bd collected all 2,039 cases: 1,935 passed, 103 skipped, and the single macOS-only os.setresgid case failed. The equivalent privilege-drop contract has separate OrbStack Linux evidence. The subsequent 852fd17e merge changed deployment and publication files rather than Controller code.
- The protocol collection failure is fixed in 571f30bd by rebuilding the tracked production wheel and refreshing its lock, SBOM and supply-chain manifest. Focused Controller tests passed 75 cases, protocol tests passed 449 with 2 skipped, and supply-chain verification passed.
- Web: build, 251 Vitest tests, 22 Chromium journeys pass with one optional screenshot capture skip at last accepted test head. New history correctness changes need their own rerun.
- Recipe package consumer: 84 packages; 477 embedded metadata entries/406 source files processed; changed-only/restart/failure/offline/fork fixture suite reported 88 passing tests.
- Rust: the exact Linux ARM64 default-stack ranged distribution consumer passes after the reusable heap-buffer fix. The newly merged NAS setup suite also passes 30 cases on OrbStack. These tests use representative bytes and do not prove OCI import, physical LAN performance or model quality.
- Deployment-channel merge checks passed 92 host-independent Compose/publication tests plus all 14 Spark bootstrap cases in OrbStack. Floating dev/latest tags remain the configured behavior; exact deployment evidence must record the resolved image IDs/digests with the source manifests.

## Next delivery sequence

Fix chart/history correctness; refresh the complete acceptance ledger without stale rows; rerun final Python/Rust/web/generated-client and appropriate Linux/container checks; final screenshot confirmation; platform PR/CI/merge; compatible Controller and agent release/deployment; recipe publication only after compatible reader deployment; actual user journeys and physical evidence. Routine approvals, merge and deployment were authorized; destructive live actions remain separate. No interactive sudo assumed.
