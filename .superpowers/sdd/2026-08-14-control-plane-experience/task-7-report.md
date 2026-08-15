# Task 7 report — Model–Recipe–Node Library projection

Status: implementation complete and locally verified on `work/control-plane-frontend-ux`. Nothing was pushed and no pull request was opened.

Exact base: `9d302684d8ad9df5416a478726f21a2e1394d94e`.

Implementation commit: `26875abb1c7eae656c10e1a033771ba1b826f1be` (`feat: add model recipe node library projection`).

## Scope

Task 7 adds two authenticated, read-only control-plane operations:

- `GET /api/v1/library`
- `GET /api/v1/library/recipes/{recipe_id}`

The implementation is confined to the six approved backend/test files. It adds no model table, migration, mutation endpoint, generated client, frontend, Rust, MIA/runtime/readiness, desired-state, dependency, lockfile, or live-system change.

Changed implementation files:

- `control/src/vonk_control/library_projection.py`
- `control/src/vonk_control/library_api.py`
- `control/src/vonk_control/api.py`
- `control/src/vonk_control/operation_api.py`
- `control/tests/test_library_projection.py`
- `control/tests/test_library_api.py`

## Contract delivered

The root projection uses a signed compound deterministic cursor over recipe slug and ID, validates `limit` in 1–100 with default 100, emits a root `next_cursor`, groups valid rows by page-local `workload.family`, and exposes invalid or unlinked rows exactly once. One family can contain many recipes; no separate model authority exists.

Root recipe rows contain bounded profile summaries and typed installation/run summaries with exact persisted recipe revision, installation, and run IDs; exact expected/covered rank counts; complete/healthy state; total/returned counts; and observable truncation. All root coverage/window subqueries are restricted to the current page recipe IDs before aggregation.

Detail projects the deterministic latest immutable revision while loading bounded operational history and exact persisted active-run lineage. Selection priority is active-run lineage, complete installed lineage, usable ready mapping/build state, then partial/history. Older referenced lineage takes precedence under the 512-row public cap.

Placement is advisory and fail-closed:

- at most 32 candidates, 512 complete groups examined, and 16 recommendations per profile;
- deterministic rank/role assignment and sorting;
- distinct `group_complete` and `search_complete`;
- exact inventory and telemetry freshness windows, reservations, active unrelated workload presence, persisted install/run state, artifact reuse, capacity headroom, fabric, and stable node IDs;
- per-node artifact evidence cap of 512, with truncation made observable and affected groups ineligible;
- stable bounded reasons, including exact admission codes, degraded run evidence, preference explanations, preview-required/single-group limitations, and truncation markers;
- typed mapping/install/run preview targets only when the exact persisted prerequisites exist;
- no fabricated IDs, URLs, implicit unload, arbitrary co-residency, or global-optimality claim.

Every unbounded schema-valid nonnegative recipe integer entering a bounded visual/profile DTO or placement arithmetic is projected through named signed-bigint saturation helpers. Saturation emits `recipe.numeric_truncated` and placement-level `projection.numeric_truncated`; an oversized node count remains unsupported and cannot become an actionable smaller group.

## Strict TDD evidence

Principal RED evidence:

| Cycle | Exact result |
|---|---|
| Initial Library API/projection collection | The planned focused command produced 2 collection errors because the two Library modules did not exist. |
| First bounded projection hardening | Focused projection regressions exposed the long-text/profile, degraded-run, uninstalled-preview, reason-cap, and older-lineage defects before production fixes. |
| Final five-finding review RED | `control/.venv/bin/pytest control/tests/test_library_projection.py -q --disable-warnings` produced 5 failed, 25 passed in 2.19s before the first root-query patch. |
| Preserved-tree checkpoint after page/health partial fix | The same command produced 3 failed, 27 passed in 2.17s: numeric saturation, complete-install priority, and per-node artifact truncation. |
| Unbounded adapter-version RED | The single numeric regression produced 1 failed in 0.44s because a schema-valid oversized adapter version violated the visual DTO. |

Final GREEN evidence:

| Command | Exact result |
|---|---|
| Numeric adapter regression | 1 passed in 0.35s. |
| Projection suite | 30 passed in 2.20s. |
| Focused projection/API/operation suite after the final code change | 51 passed, 15 warnings in 4.13s. |
| Broader Library/catalog/recipe/Fleet/API regression slice | 199 passed, 1 skipped, 15 warnings in 10.99s. |
| Explicit in-memory OpenAPI parity slice | 3 passed in 0.86s. |

The broader command covered the three focused files plus `test_api.py`, catalog API/repository/service, recipe API/contract/operations, and Fleet projection/events/stream. The one skip is the existing environment-gated Docker/PostgreSQL test; no live system was contacted.

## Fixed query-bound proof

`test_root_operational_summaries_are_exact_bounded_and_fair_per_recipe` records exactly 6 root SELECTs after review fix round 1. The installation and run windows are restricted to current page recipe IDs, then three set membership queries feed the shared pure coverage/health predicates. The test proves 65 history rows for one recipe cannot hide another recipe's current state.

`test_detail_uses_a_fixed_set_query_count_instead_of_candidate_services` records exactly 21 detail SELECTs before and after increasing the candidate pool from 2 to 12 nodes. Seven added set queries load fail-closed current operational evidence separately from bounded displayed history. Artifact, reservation, older-lineage, and operational evidence regressions remain within the same 21-query bound.

## Static and contract verification

| Command | Exact result |
|---|---|
| `uvx ruff@0.16.1 check` on all six changed Python files | All checks passed. |
| `uvx ruff@0.16.1 format --check` on the four new files | 4 files already formatted. |
| `control/.venv/bin/python -m compileall -q` on all six changed files | Exit 0, no output. |
| In-memory OpenAPI tests | Library operation IDs, typed models, bounded errors, and bearer security passed; no tracked OpenAPI/client generation occurred. |
| `git diff --check` and `git diff --cached --check` before implementation commit | Exit 0. |

`api.py` and `operation_api.py` contain pre-existing whole-file Ruff formatting debt outside Task 7. Their Task 7 additions are minimal and Ruff lint-clean; formatting those full legacy files would have created unrelated rewrites, so the format gate was applied to the four Task 7-owned files.

## Concerns and limitations

1. Placement is intentionally bounded advisory evidence. Artifact evidence truncation, candidate/group truncation, stale/missing evidence, or unsupported node count prevents an exhaustive or optimistic claim and requires authoritative preview.
2. Root summaries cap each recipe independently at 64 installations and 64 active runs. Detail operational collections cap at 512 rows while prioritizing active and complete exact lineage; truncation counts/reasons remain explicit.
3. No generated OpenAPI document or clients were produced. That remains a controller action after contract approval.
4. The implementation commit used the workstation's existing automatically configured Git committer identity; no Git configuration was changed.

## Independent review fix round 1

The independent review reported four Important findings. Implementation commit `6753fd1b0ea45ad7a81488b6000dc393a7505c87` (`fix(control): align Library operational parity`) addresses all four without expanding Task 7 scope.

1. Root and detail now call one pure installation-coverage predicate. It requires installation state `installed`, declared mapping cardinality, duplicate-free exact node/rank/role membership, and every corresponding installation-node state `installed`. `installed_bytes` remains informational. Parity tests exercise low byte evidence plus duplicate and missing ranks against `RunAdmissionService.plan_run()` and staged run targets.
2. Root and detail now call one pure rank-health predicate with exact declared mapping membership, every rank `running`, and evidence age strictly below 300 seconds. Aggregate run and route states remain separately projected. Pending, failed, and withdrawn routes use separate warning codes and do not produce `run.degraded` when rank health matches `RecipeOperationService.run_status()`.
3. Placement now has a separate seven-query current-evidence phase with 512-row and 16,384-member limits plus typed observed counts. Any relevant truncation makes search incomplete and affected/all possibly affected groups ineligible, suppresses install/load claims and install/run targets, and emits `projection.evidence_truncated`. A 513-active-run exact-lineage regression proves fail-closed behavior.
4. Display-only parameter scalar strings are capped at 512 characters and signed integers at signed-bigint bounds; booleans/null remain typed. Truncation is observable through `recipe.display_scalar_truncated` and `recipe.numeric_truncated`. Default-profile mapping targets submit `parameters={}`, allowing `ClusterMappingService` to apply immutable declared overrides exactly rather than receiving bounded display copies.

The fixtures now persist realistic run plan membership. Additional coverage proves one family across recipe pages and signed compound cursor stability, tamper rejection, and query binding.

Review-round RED/GREEN and final verification:

| Command / phase | Exact result |
|---|---|
| New projection regressions before production fixes | 8 failed, 30 passed in 2.74s. |
| First implementation attempt | 2 failed, 36 passed in 2.82s; failures isolated to the old query-count assertion and missing declared mapping cardinality. |
| Final projection suite | 38 passed in 2.68s. |
| Focused Library projection/API/operation suite | 59 passed, 15 warnings in 4.64s. |
| Broader Library/API/catalog/recipe/admission/mapping/Fleet slice | 212 passed, 2 skipped, 15 warnings in 12.39s. |
| Explicit in-memory OpenAPI parity | 3 passed in 1.01s. |
| `uvx ruff@0.16.1 check` on all six Task 7 Python files | All checks passed. |
| Pinned Ruff format check on the four Task 7-owned files | 4 files already formatted. |
| Compile and diff checks | Exit 0 with no output. |

The two skips are existing environment-gated PostgreSQL/Docker tests. No live infrastructure was contacted, and no generated client, frontend, Rust, MIA/runtime/readiness, dependency, migration, push, or PR action occurred.
