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
