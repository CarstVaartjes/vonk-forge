> Historical audit: the owner removed count and category ledgers on 2026-10-08.
> Its old accounting commands are retired; current policy is in principle-guards.md.

# Request validation and recipe-build debt review

This review uses the working-tree blocker registry, not the older audit's 508
raise snapshot. A validation credit requires a caller-supplied value and a
rejection before any effect. An error-class name or a retryable flag is not proof.

## Reviewed families

| Module / family | Decision and evidence |
| --- | --- |
| `catalog_entities.exact-reference-request` | Reclassify `resolve_reference`'s absent exact active target: the caller supplies the reference and the function only reads before rejecting it. |
| `catalog_entities.bookkeeping-debt` | Retain missing persisted revision/head and model artifact/reference projections. `_head`, `revise`, and `_bind_recipe_models` consume stored state; their absence is not proof of invalid caller input. |
| `catalog_service.bookkeeping-debt` | Retain unavailable bundle storage and missing imported document/head. `store_source_bundle` needs operational storage; `_select_imported_recipe_head` reads retained records inside an import that already upserted dependencies. |
| `catalog_service.input-validation` | Existing canonical import document, digest, publication/release, actor and package-handle checks precede the import transaction. No additional debt site qualifies. Stored `_view` and retained-head checks are not newly credited by this review. |
| `compiled_execution_plan.receipt-request` | Reclassify `_verified_model_object` and `compile_verified_execution_plan` consistency rejection. The pure compiler checks supplied receipt values before returning a dispatch document, without storage, reservations or dispatch. It does not discover missing cache objects. |
| `recipe_runtime_specs.bookkeeping-debt` | Retain both missing binding and missing telemetry after `compile_canonical_harness`. These are internally generated projection omissions, not malformed caller values. |
| `recipe_runtime_specs.input-validation` | Existing role/rank/options/parameters/package-path validation checks supplied compile inputs before producing the runtime spec. No internal omission receives this credit. |
| `canonical.image-handle-request` | Reclassify the two `_image` checks: the pure compiler receives the package/build handle as an argument and rejects a missing digest or inconsistent reference before returning a projection. Actual storage availability and ingress digest verification remain separate. |
| `common.projection-request` | Reclassify missing built-in telemetry on the supplied standalone projection. The production canonical compiler passes `canonical_argv=True`, so `enforce_engine_contract` is false and this branch cannot represent its internal telemetry omission. The helper validates caller-supplied projection data before any effect. |
| Other harness validation/security families | Existing argv/settings/role/rank/environment/mount input checks are pre-launch compilation. Path, socket and security-envelope fences remain enforced. No new credit is taken for internally generated state. |
| `source_bundles.bookkeeping-debt` | Retain both stores' missing bundles, storage unavailability, and `_generated_bundle` / `_inspect_archive` read failures. Archive helpers are shared with reads of persisted bytes; they are not exclusively ingress validation. |
| `source_bundles` ingress families | Existing supplied digest, binary payload, size and archive-shape checks run before publication. Path safety and digest verification at ingress remain security fences. Shared stored-read failures do not move with them. |
| `contract_graph.input-validation` | Already classified: supplied application route/schema declarations and wire exports are checked during pure discovery before a usable contract graph is returned. No remaining debt to reclassify. |
| `operation_api` wiring | `admin_openapi_schema` validates the supplied app's operation IDs/component declarations before returning a schema. Its two builtin `RuntimeError` raises are outside the error-family inventory; no debt reduction is claimed for them. Runtime activation/publication/profile projection failures consume persisted/live evidence and are not request validation. The three listed projection debt raises remain. |
| `catalog_sync._validate_request` | Already classified: UUID, trigger, actor and expected commit are supplied to `sync`, which invokes validation before creating the sync run or contacting the reader. The later repository-changed observation remains debt. |

Existing mixed input families are not newly promoted wholesale. Only the six
explicit pre-effect sites above move from debt to validation; the registry gives
each moved family its own written reason.

## Recipe-build recovery

The remaining eleven counted raises previously had unproven paths through
broad handlers or the structural build-resolution method:

- Production preparation and persistence now catch `UnknownOutcomeError`
  explicitly. Uncertain persistence rolls back; the returned `BuildUnsettled`
  retains its typed reason and explicitly permits retry. The identity-lock
  runner releases its locks and `_fail` hands the observation to the existing
  bounded lifecycle backoff. Security and invalid-request errors propagate to
  their owning category handlers instead of becoming a generic planning miss.
- Library image planning and source-policy reporting explicitly end an unknown
  recipe entry with a diagnostic. They retain no build claim or reservation;
  other recipes and a fresh invocation remain eligible. Their typed handlers
  are registered as non-blocking observation ends.
- An invalid recorded cached-builder digest produces no content identity.
  `reusable_build` treats it as a cache miss; the same resolution immediately
  accepts a valid receipt. The removed raise is not reclassified as input.

The all-path retry proof now credits the ten remaining raises. One raise was
removed. The recipe-build guard scans every active unknown raise and requires a
proven handler; deleting the production registration invalidates persistence
credit. The cached-identity regression also exercises immediate fresh reuse.
No PostgreSQL schema or wire model changes are needed.

## Weighted bookkeeping debt

| Module | Before | After |
| --- | ---: | ---: |
| catalog_entities | 5 | 4 |
| catalog_service | 3 | 3 |
| compiled_execution_plan | 2 | 0 |
| recipe_runtime_specs | 2 | 2 |
| harnesses/canonical | 2 | 0 |
| harnesses/common | 1 | 0 |
| source_bundles | 5 | 5 |
| contract_graph | 0 | 0 |
| operation_api | 3 | 3 |
| catalog_sync | 1 | 1 |
| recipe_builds | 11 | 0 |
| Reviewed total | 35 | 18 |
| Repository total | 214 | 197 |

the retired category-rebalance and baseline-writing commands
reconcile the registry with the proof. No real debt is relabelled. File-size
ceilings for the two shrinking source modules are lowered too. Repository,
CI, deployed Controller and physical Spark evidence remain separate; this
track does not access production or run local integration suites.

## Source anchors and verification

Current source anchors (paths are under `control/src/vonk_control`):
`catalog_entities.py:407`, `catalog_service.py:454`,
`compiled_execution_plan.py:440,467`, `recipe_runtime_specs.py:161`,
`harnesses/canonical.py:149`, `harnesses/common.py:64`,
`source_bundles.py:397,431`, `contract_graph.py:158`,
`operation_api.py:2345`, `catalog_sync.py:735`, `recipe_builds.py:575`,
`availability_production.py:777,895,953,961,1240`, and
`prebuilt_images.py:304,444`.

- Required Controller static guards plus the focused unknown-planning regression:
  **305 passed**.
- `scripts/test guards`: **218 passed**.
- Explicit data-contract, destructive-shell, workflow-environment and Git
  hermeticity guards: **43 passed**.
- Wait/remedy scans: **223 / 24 sites, zero violations**.
- Ruff and format checks over all eight changed Python files: passed.
- Full `scripts/check-python-types`: **one existing reviewed exception, no
  unlisted errors**. The installed recipe contract was stale; verification used
  a read-only export of the exact locked recipe-contract commit
  `bd5090708a13b7936a4c17bc97db7b1c8967e23a` outside the repository, with local
  Controller/protocol sources first on `PYTHONPATH`. No environment was added to
  the working tree.
- the retired allowance-history check: passed.
- Cached-identity regression with the original raise restored: **failed as
  expected**; the fixed version passed in the final guard run.
- `git diff --check`: passed. No Rust changes or new contract-word literals.

Local integration/full suites, PostgreSQL and physical acceptance were not run,
per this track's instruction. The coordinator owns the commit and post-commit
the retired lifecycle-count report check because `.git` is read-only.
The report currently describes unchanged HEAD; the working-tree count report
records the intended after values below:

```text
before: writers=0 operator_waits=3 raises=2137 debt=214
after: writers=0 operator_waits=3 raises=2136 debt=197
```
