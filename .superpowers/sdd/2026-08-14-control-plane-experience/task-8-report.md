# Task 8 implementation report (in progress)

Correct base: `fc7cc588203dd2ec69b43ef861577a959e6ca325`.

## Checkpoint 1 — Library foundation and guided actions

Implementation commit: `924834b` (`feat(control-web): add previewed Library actions`).

This checkpoint contains the accumulated, previously uncommitted Library foundation and the bounded action-dialog slice. It does not claim Task 8 complete.

### Implemented scope

- Added URL-addressable `/library`, model, recipe, and unlinked navigation while retaining all legacy catalog/editor/source/mapping/import routes.
- Added generated-contract-backed, distinctly named Library API methods for snapshot/detail, Mapping/Install/Load/Stop/Remove preview and apply, operation status/retry, run status, and job progress.
- Added visual immutable-recipe, topology/resource, freshness, typed-reason, and complete atomic placement-group views.
- Added explicit server-preview dialogs for Mapping, Install, Load, Stop, and Remove. Every apply submits the reviewed digest and exact selected owner/input.
- Load always states: “Existing recipes remain loaded. Forge will not unload anything automatically.” Coexistence appears only when a server warning explicitly supplies it.
- Stop keeps complete-rank and reservation authority visible and does not describe capacity as released before complete success.
- Remove shows active-run blockers, unknown-byte authority, all typed reasons, no automatic Stop, catalog retention, and reinstall consequences. There is no catalog-delete control.
- Added grouped operation state/job progress, terminal detail refresh, partial/failed incompleteness, authoritative retry, status-error retry, and timer cleanup.
- Selection is retained across apply/refresh. Dialog Escape/close returns focus to the invoking action; stale apply rejection keeps the dialog open, disables Apply, and requires a fresh preview.

### TDD evidence

RED was observed before the initial action integration:

- The first action suite reported `3 failed (3)` because selected groups had no `Review Load` action and operational rows had no Stop/Remove actions.
- After adding Mapping/Install and preview-error coverage, the suite reported `5 failed (5)` on the same missing action boundary.
- The first TypeScript build failed with `TS2554: Expected 1 arguments, but got 0` for the trigger ref; initializing it explicitly fixed the compile contract.
- The stale-preview regression failed because `Load selected installation` remained enabled after `preview_digest_stale`.
- The uninstall-reason regression failed because `uninstall.bytes_unknown` had been visually summarized but filtered from the typed blocker list.
- Poll-cleanup sensitivity was verified by temporarily removing timer cancellation: the focused regression failed with five operation calls instead of three after unmount; restoring cleanup returned it to GREEN.

GREEN behavior is covered by `library-actions.test.tsx`: distinct Mapping/Install apply payloads, Load no-unload and conditional coexistence copy, stale preview refresh, Stop/Remove consequences and apply digests, partial grouped job progress/retry, preview/status error retry, focus return, and polling cleanup.

### Verification at checkpoint

- Focused Vitest: `4 passed (4)` files, `29 passed (29)` tests.
- Full Vitest: `27 passed | 1 skipped (28)` files, `141 passed | 1 skipped (142)` tests.
- Build: TypeScript and Vite passed; 64 modules transformed.
- `git diff --check`: clean.
- Scope audit: only handwritten `control/web/src` source/tests changed. No backend, generated client, Rust, recipes/runtime/readiness, migrations, dependencies, e2e, or live systems were changed.

### Explicitly deferred

- Advanced raw JSON editing/upload and last-valid visual preview.
- Responsive Library CSS/reflow work.
- `fleet-library.spec.ts` expansion and four-width browser verification.
- Final Task 8 report completion and final integration verification.

## Task 8A review fix round 1

Implementation commit: `31cc27576f81bdcce8654e189f9072612e506092` (`fix(control-web): address Library action review`).

This round started from clean HEAD `744a810981ffa11570a5764eb651215b937bcbf0` and addresses all seven Important findings in `task-8a-review-round-1.md`. The generated alias contract already present at that base made the previewed alias digest-bound and replay-safe; no backend or generated-client edit was required in this round.

### Fixes delivered

1. Mapping review now carries the selected complete-group evidence into the preview and renders per-node disk requirements, active reservations, post-action headroom, exact artifact reuse, inventory freshness, and typed placement reasons. Install review explicitly classifies inventory as fresh, stale, or unavailable relative to the published freshness policy and preserves typed stale/unavailable blockers and warnings.
2. Load review visibly displays `RunPlanResponse.alias`, and apply uses that exact preview response alias with its plan digest.
3. Operation polling now obtains and publishes grouped job progress, performs the terminal recipe-authority refetch, and only then publishes terminal operation state. The deterministic deferred-promise regression proves effect cleanup can no longer discard job progress or terminal refresh.
4. Every handwritten Library preview/apply/operation/run/job wrapper accepts and forwards `AbortSignal`. Preview and poll effects own abort controllers; imperative apply and operation retry paths abort on unmount and guard every post-await update/callback.
5. The dialog creates one request key for each preview attempt. Mapping, Install, Load, Stop, and Remove apply payloads reuse that key after ambiguous errors; requesting a fresh preview rotates it. The client no longer silently invents a new apply key.
6. Current operation authority and complete-group action controls now lead topology/provenance detail, selected-group actions precede secondary evidence, unavailable placement evidence is collapsed, and the focused `library.css` module supplies the base desktop three-pane hierarchy at widths of at least 900px.
7. Library pagination exposes an authoritative Load more control, forwards the cursor and abort signal, merges recipes into an existing model family without duplication, preserves unlinked grouping, and continues until `next_cursor` is absent.

### Strict TDD evidence

The focused RED command was:

`npm test -- --run src/api/client.test.ts src/components/library-actions.test.tsx src/pages/library.test.tsx`

Observed RED before implementation: 3 files ran; 2 failed and 1 passed. Seven tests failed and 20 passed. The exact failures were missing endpoint alias display, missing Mapping capacity/freshness evidence, absent stable request key, missing `AbortSignal` on deferred job polling, absent preview cancellation, old action/evidence hierarchy, and absent Load more pagination.

After implementation, the same focused command passed 3 files and all 27 tests. Added coverage also verifies typed stale Install reasons, apply/retry abort on cleanup, fresh-preview key rotation, exact apply aliases and digests, and same-family plus unlinked second-page merging.

### Final verification

- Focused Vitest: 3 files passed; 27 tests passed.
- Full Vitest (`npm test -- --run`): 27 passed and 1 skipped files; 145 passed and 1 skipped tests.
- Build (`npm run build`): TypeScript and Vite passed; 65 modules transformed; production assets emitted successfully.
- `git diff --check`: clean before the implementation commit.
- Scope audit: the implementation commit contains only handwritten `control/web/src` source, tests, and the focused Library stylesheet. It changes no backend, generated declaration, Rust, MIA recipe/runtime/readiness, migration, dependency, e2e, or live-system file.

### Still deferred by instruction

- Advanced JSON editing/upload and last-valid visual preview.
- Responsive behavior below 900px and four-width browser work.
- `control/web/e2e/fleet-library.spec.ts` changes and E2E execution.

## Task 8A review fix round 2

Implementation commit: `ccf9e56f610bf7cf73bbe2fb4cd15658f4f13b11` (`fix(control-web): bind Library refresh authority`).

This round fixes only the two remaining Important findings from `task-8a-review-round-2.md`.

### Fixes delivered

1. Install freshness no longer uses `LibraryRecipeDetail.generated_at`. The action dialog records when each server preview response is received, and Install classifies observation age against that preview-receipt time. Typed `inventory.stale` and `inventory.unavailable` preview evidence takes precedence, so the UI cannot claim fresh when the server supplied contrary authority. Typed unavailable evidence is described truthfully even when an observation timestamp is present.
2. Apply-owned and terminal-poll refreshes now pass their owning `AbortSignal` through every `onRefresh` boundary into `libraryRecipe`. `LibraryPage.refreshDetail` checks that signal before requesting, after awaiting, before detail/error state updates, and terminal/apply callbacks retain their post-await abort guards.

### Strict TDD evidence

- Install preview-time RED: `npm test -- --run src/components/library-actions.test.tsx -t "applies Mapping and Install only"` failed because the old detail timestamp rendered both later observations as `Inventory fresh · 0s` instead of typed/receipt-time stale states. A second RED for typed unavailable evidence failed on the false `observation not reported` wording while an observation timestamp was present. The focused test passed after each minimal fix.
- Refresh-cleanup RED: `npm test -- --run src/components/library-actions.test.tsx -t "detail refresh"` ran two regressions and both failed because apply and terminal refresh callbacks received `undefined` instead of an `AbortSignal`. GREEN proves closing/unmounting aborts the deferred detail request, its late response cannot overwrite the still-mounted page, and terminal `onChange` is not called after late completion.

### Final verification

- Focused Vitest: 3 files passed; 29 tests passed.
- Full Vitest (`npm test -- --run`): 27 passed and 1 skipped files; 147 passed and 1 skipped tests.
- Build (`npm run build`): TypeScript and Vite passed; 65 modules transformed.
- `git diff --check`: clean before the implementation commit.
- Scope audit: seven handwritten Library source/test files changed. No Task 8B UI, backend, generated client, Rust, dependency, migration, e2e, live-system, Advanced JSON/upload, or responsive-below-900px file changed.
