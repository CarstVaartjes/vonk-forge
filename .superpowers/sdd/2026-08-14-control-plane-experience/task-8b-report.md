# Task 8B report — responsive Library, Advanced editor, and browser acceptance

Date: 2026-08-15

Status: **DONE**

Task 8B was implemented from clean base `1eb5e80b71e6c4b7938c5c836be3ce9de5742509` on `work/control-plane-frontend-ux`. The checkpoint remains frontend-only and builds on the approved Task 8A authority model. No backend authority, generated API, dependency, lockfile, Rust, MIA, runtime, readiness, migration, or live-system change is included.

## Delivered behavior

- At 900 CSS px and wider, Library renders coordinated Models, Recipes, and exact recipe-authority panes. At 899 px and narrower, the same routes become a one-pane drill-down with explicit Back links and browser-history-preserving navigation.
- The responsive layout has no body or document overflow at 320, 360, 768, 899, 900, 1280, or 1920 CSS px. Focus order remains DOM-logical, row selection occurs only on activation, route changes focus the Library heading, controls preserve visible focus, and polling/live updates do not take focus.
- A recipe reached from the explicit Unlinked list retains that list as its selected parent and exposes `Back to Unlinked recipes` with `/library/models/~unlinked`, including after the detail route is active.
- Raw canonical JSON editing and JSON-file upload live in an Advanced `<details>` section after the visual authority. Valid local edits update only the visual preview; they do not save, resolve, apply, or publish. Source/build, mapping, and raw-editor workflows remain directly linked.
- JSON syntax and strict canonical-schema failures are associated with both editor and upload controls and identify a useful field path. Invalid input preserves the last valid visual preview and leaves keyboard focus on the invoking control.
- The handwritten parser mirrors the bounded canonical visual schema, including forbidden extra fields, nested object shape, exact enums/digests, string and list bounds, integer ranges, and all projected fields.
- JavaScript-unsafe integers are never silently rounded into a visual preview. The parser reports the precise field path and the exact preservable range, for example `$.build.download_bytes cannot be preserved exactly; use an integer from 0 through 9007199254740991.`
- Advanced draft, validation, and last-valid state reset when canonical recipe identity, selected revision identity/digest, or canonical visual content changes. Regressions prove a same-revision server refresh and A→B→A navigation cannot resurrect an old local draft.
- Snapshot and recipe-detail failures have local retry paths, and the empty Library has an escape hatch to the advanced catalog.

## Strict TDD evidence

Implementation proceeded through focused RED/GREEN cycles rather than retrofitted coverage:

- Responsive tests initially exposed the missing below-900 pane behavior and exact 900 px boundary.
- Canonical parsing began with the module absent, then seven structural/schema failures. Advanced workflow tests began with two missing-component failures. Recovery, contrast, fixture routing, and console-cleanliness assertions each produced focused failures before their minimal fixes.
- A first parity review exposed canonical validation that was still too permissive and stale preview state when switching recipes in one mounted authority component. Focused RED was 11 failed and 10 passed before strict-schema and recipe-switch fixes.
- The final three Important concerns were encoded as regressions before implementation. Focused Vitest was 3 failed and 25 passed: an unsafe integer was accepted after rounding, a same-revision canonical refresh retained the local preview, and unlinked detail lost its list region/Back context.
- The corresponding focused Playwright journey failed 1/1 because `Back to Unlinked recipes` did not exist on the unlinked detail route.
- Minimal fixes added exact-number rejection, canonical preview-state invalidation/remounting, and inferred unlinked parent context. The three focused Vitest files then passed 28/28, and the focused responsive Playwright journey passed 1/1.

The concerns were not handled by relaxing canonical validation: unsafe numeric input remains rejected, strict unknown-field and bound checks remain active, and only valid documents can replace the last-valid preview.

## Final verification

All commands ran in `control/web` with `NO_COLOR` and `FORCE_COLOR` removed where relevant so Playwright emitted no conflicting-color warning.

- Focused Vitest:
  `npm test -- --run src/lib/library-recipe-document.test.ts src/components/library-recipe-advanced.test.tsx src/components/library-actions.test.tsx src/pages/library.test.tsx`
  — 4 files passed, 39 tests passed.
- Full Vitest:
  `npm test -- --run`
  — 29 files passed, 1 skipped; 172 tests passed, 1 skipped.
- Production build:
  `npm run build`
  — TypeScript passed and Vite transformed 68 modules successfully.
- Full fixture Playwright:
  `npm run test:e2e`
  — 8 tests passed. Every test records console warnings, console errors, and page errors and asserts the collection is empty.
- Responsive browser assertions cover 360, 768, 1280, and 1920 CSS px as required, plus 320, 899, and the exact 900 px breakpoint. The journey also covers URL/back navigation, multiple recipes, unlinked detail, complete-group authority, bounded/freshness evidence, action cancel/focus return, Load no-unload copy, partial retry, Advanced invalid/valid/upload recovery, error retries, empty state, and cleanup.
- `git diff --check` and the staged diff check passed before commit.

## Visual inspection

Fresh local fixture screenshots were inspected after the final E2E run:

- `library-mobile.png` at 360 px shows one clear detail pane, an unobscured `Back to Qwen 3 recipes` control, readable authority hierarchy, and controls contained within the viewport.
- `library-desktop.png` at 1280 px shows three aligned panes, immediate model/recipe selection state, a visually primary authority panel, readable partial-operation evidence, and no clipping or overlap.

The automated contrast assertion for the authority pane is at least 4.5:1.

## Scope and safety audit

- `control/web/package-lock.json` was restored to HEAD and is unchanged. No dependency or `node_modules` artifact is staged.
- The bounded change consists only of handwritten Library source, focused styles, Vitest tests, fixture-backed Playwright coverage, and these Task 8 reports.
- Tests used only local route fixtures. No request or mutation targeted Tailscale, NAS, Sparks, MIA, runtime, readiness, or another live system.
- No push was performed.

## Concerns and disposition

The three final Important concerns—unsafe integer rounding, stale Advanced state across canonical refresh/navigation, and lost unlinked-list Back context—are resolved and covered by RED-first regressions. The visual/editor path intentionally accepts only integers JavaScript can preserve exactly; larger otherwise-canonical signed integers receive a precise field-path error instead of being rounded. No known blocker or unresolved Task 8B concern remains. Per the finalization instruction, no additional broad self-review loop was started after the complete green verification sweep.

## Task 8B review fix round 1

This round starts from Task 8B checkpoint `8db578c4aba0e8e1f4e65357661213f94be15d5b` and addresses all five Important findings in `task-8b-review-round-1.md` without reopening Task 8A or backend authority.

### Fixes delivered

1. Canonical integer validation now walks JSON tokens and their object/array paths before `JSON.parse` can erase numeric spelling. Integer fields reject decimal, exponent, and underflowing-exponent literals with their exact field path. Plain integer tokens are compared as `BigInt`, preserving both signed-bigint range errors and the separate JavaScript exact-preservation error for otherwise-canonical unsafe values.
2. Advanced no longer uses a changing React `key`. Its controlled draft and error state reset from an explicit canonical token composed from recipe identity, revision identity/digest, and visual content. Canonical refresh, same-revision content change, and A→B→A reset correctly without replacing the `<details>`, textarea, or upload node; polling-only detail updates retain draft and focus.
3. Every field of the valid local visual document now drives the visible preview: schema version; identity; metadata title, description, and tags; workload; build context, network, byte budgets, and timeout; all artifact fields; runtime interface, adapter, endpoint, aliases, and health path; provenance; and validation. A warning pill labels `Local preview · not saved`. Placement, operation state, revision lifecycle, and action preview/apply aliases remain canonical server authority.
4. Single-pane behavior is now the base CSS cascade. Only `@media (min-width: 900px)` overrides it with the coordinated desktop panes, eliminating the theoretical fractional gap between legacy max/min queries. Fixture Playwright exercises an actual 899.5px iframe and the no-matching-rule cascade, plus 899 and 900 px.
5. Cursor merging is bounded to 40 models and 50 recipes per linked/unlinked list. The merge retains the active model or recipe when older rows leave the window, keeps Unlinked navigation available, continues using the authoritative server cursor, and displays an honest bounded-window message including whether more server pages remain.

### Strict RED→GREEN evidence

- Numeric lexemes: the focused parser run failed 3 tests and passed 18 because `1000.0`, `1e3`, and `1e-9999` were accepted as 1000, 1000, and 0. A follow-up range regression failed because pre-parse precision handling initially masked the signed-bigint maximum. The final focused parser run passed 22/22, including unsafe-in-range and out-of-range literals.
- Advanced reset/focus: the focused regression failed because canonical refresh replaced the open Advanced group, dropping textarea focus. After replacing remounting with the reset token, the focused regression passed; the complete Advanced file passed 4/4 at that cycle. It proves textarea and upload focus survive canonical changes, state resets, polling-only refresh preserves an unchanged draft, and A→B→A cannot revive stale state.
- Complete preview: the focused regression failed at the absent `Local preview · not saved` state while canonical hero/build fields remained visible. It passed after the focused visual projection was added. The combined Advanced and Library run passed 11/11, including the existing canonical action-alias guard.
- Fractional breakpoint: after correcting a test-harness cleanup mistake, the valid browser RED failed because Models remained visible when neither legacy media rule applied at the 899.5px boundary. The same responsive journey passed 1/1 after making mobile behavior the default cascade.
- Bounded pagination: a four-page fixture accumulated 80 linked recipes, 80 unlinked recipes, and more than 40 models; RED failed because there was no bounded-window status and all rows remained. GREEN passed with capped rows, pinned `Pinned Recipe`, reachable latest linked/unlinked recipes, and the terminal `No more server pages` state.

### Fix-round verification

All commands ran in `control/web` with conflicting color variables removed.

- Focused Vitest:
  `npm test -- --run src/lib/library-recipe-document.test.ts src/components/library-recipe-advanced.test.tsx src/components/library-actions.test.tsx src/pages/library.test.tsx`
  — 4 files passed; 45 tests passed.
- Full Vitest:
  `npm test -- --run`
  — 29 files passed and 1 skipped; 178 tests passed and 1 skipped.
- Production build:
  `npm run build`
  — TypeScript passed; Vite transformed 69 modules and emitted production assets.
- Full local fixture Playwright:
  `npm run test:e2e`
  — 8/8 passed, including the fractional breakpoint. Console warnings, console errors, and page errors remained empty.
- `git diff --check` passed. `control/web/package-lock.json` is unchanged, and no dependency or `node_modules` artifact is included.

The review's one Minor finding—large stylesheet and combined Fleet/Library E2E files—is deliberately deferred to final branch review as instructed. No unrelated file split or refactor was performed. No live system was contacted and no push was performed.
