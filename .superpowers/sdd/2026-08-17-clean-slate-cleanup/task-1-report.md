# Task 1 implementation report

Date: 2026-08-17
Task: Remove superseded navigation and page routes
Commit: `f9b74a0a`

## Changed files

- `control/web/src/app.tsx`
- `control/web/src/components/app-shell.test.tsx`
- `control/web/src/components/app-shell.tsx`
- `control/web/src/components/library-recipe-detail.tsx`
- `control/web/src/components/recipe-summary.tsx`
- `control/web/src/pages/fleet.tsx`
- `control/web/src/pages/library.test.tsx`
- `control/web/src/pages/library.tsx`

## Deleted files

- `control/web/src/pages/agents.tsx`
- `control/web/src/pages/agents.test.tsx`
- `control/web/src/pages/catalog.tsx`
- `control/web/src/pages/catalog.test.tsx`
- `control/web/src/pages/recipe-editor.tsx`
- `control/web/src/pages/recipe-editor.test.tsx`
- `control/web/src/pages/recipe-source.tsx`
- `control/web/src/pages/workload-run-import.tsx`
- `control/web/src/pages/workload-run-import.test.tsx`
- `control/web/src/pages/cluster-mapping.tsx`
- `control/web/src/pages/packages.tsx`
- `control/web/src/pages/packages.test.tsx`
- `control/web/src/pages/package-candidate.tsx`
- `control/web/src/pages/package-types.ts`
- `control/web/src/pages/deployments.tsx`
- `control/web/src/pages/deployments.test.tsx`
- `control/web/src/pages/updates.tsx`
- `control/web/src/pages/updates.test.tsx`
- `control/web/src/pages/jobs.tsx`
- `control/web/src/pages/jobs.test.tsx`
- `control/web/src/pages/audit.tsx`

## Tests run

1. `cd control/web && npm test -- --run src/components/app-shell.test.tsx`
   - Initial result: FAIL
   - Failure confirmed the new contract test caught legacy navigation still rendering `Agents`

2. `cd control/web && npm test -- --run src/components/app-shell.test.tsx`
   - Result after implementation: PASS
   - `Test Files  1 passed (1)`
   - `Tests  7 passed (7)`

3. `cd control/web && npm test -- --run`
   - Result: PASS
   - `Test Files  18 passed | 1 skipped (19)`
   - `Tests  115 passed | 1 skipped (116)`

## Remaining import/route checks

- Searched retained `control/web/src` files for imports of deleted page modules
- Searched retained `control/web/src` files for route links to deleted `/agents`, `/catalog`, `/packages`, `/deployments`, `/updates`, `/jobs`, and `/audit` pages
- Result: no remaining retained imports or route links found

## Concerns

- The implementation stayed within Task 1 scope, but clearing retained links to deleted pages required updating a few supporting retained files and tests beyond the four top-level modified files listed in the brief.
- The report file was written after the required source commit, so it is not included in commit `f9b74a0a`.

## Fix round 1 — 2026-08-17

Addressed review findings:

1. Removed the orphaned package/deployment surface left behind after Task 1:
   - deleted `control/web/src/components/package-inventory.tsx`
   - removed the orphaned package/deployment helper methods from `control/web/src/api/client.ts`
   - removed the corresponding `ControlApi` package inventory/gc/removal methods from `control/web/src/api/types.ts`
   - removed the old package/deployment client test block from `control/web/src/api/client.test.ts`
2. Removed the `/catalog` compatibility fallback:
   - unsupported legacy paths no longer render Fleet or Library content
   - `/` still opens Fleet normally

### Fix-round changed files

- `control/web/src/api/client.test.ts`
- `control/web/src/api/client.ts`
- `control/web/src/api/types.ts`
- `control/web/src/app.tsx`
- `control/web/src/components/app-shell.test.tsx`
- `control/web/src/components/app-shell.tsx`
- Deleted: `control/web/src/components/package-inventory.tsx`

### Fix-round commands, tests, and exact output

1. Focused red run after adding the two regression tests:

   Command:
   `cd control/web && npm test -- --run src/components/app-shell.test.tsx src/api/client.test.ts`

   Result:

   ```text
   Test Files  2 failed (2)
        Tests  2 failed | 21 passed (23)
   ```

   Key failures:
   - `does not expose orphaned package and deployment helpers after the Fleet/Library cleanup`
   - `does not render a replacement page for unsupported legacy catalog URLs`

2. Focused green run after implementing the cleanup and narrowing `/` back to Fleet:

   Command:
   `cd control/web && npm test -- --run src/components/app-shell.test.tsx src/api/client.test.ts`

   Exact output:

   ```text
   Test Files  2 passed (2)
        Tests  22 passed (22)
   Start at  23:54:55
   Duration  928ms (transform 201ms, setup 70ms, import 262ms, tests 286ms, environment 693ms)
   ```

3. Orphan-reference sweep:

   Command:
   `rg -n "package-inventory|packageInventory\(|previewPackageGc\(|applyPackageGc\(|previewPackageRemoval\(|removePackageInventory\(|deployments\(|previewPackageRollout\(|startPackageRollout\(|packageRollout\(|previewPackageRollback\(|rollbackPackage\(|packageFamilies\(|packageCandidates\(|packageCandidate\(|previewPackageValidation\(|validatePackage\(|packageValidation\(|previewPackagePromotion\(|promotePackage\(" control/web/src`

   Exact result:

   ```text
   no matches
   ```

4. Full web verification:

   Command:
   `cd control/web && npm test -- --run`

   Exact output:

   ```text
   Test Files  18 passed | 1 skipped (19)
        Tests  115 passed | 1 skipped (116)
   Start at  23:55:05
   Duration  3.94s (transform 2.30s, setup 1.37s, import 3.90s, tests 8.69s, environment 11.73s)
   ```

### Fix-round concerns

- The unsupported-route change is intentionally narrow: `/` still resolves to Fleet, while legacy non-retained paths like `/catalog` now render no page content and no active primary route.
