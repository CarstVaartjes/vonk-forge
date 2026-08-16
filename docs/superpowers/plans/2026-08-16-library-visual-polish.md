# Library and Catalog Visual Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the v1 Library and Catalog feel like a finished Vonk Forge product while preserving the existing dark-green main-branch visual language and exact recipe authority.

**Architecture:** Keep the current API, routing, and accessibility semantics. Add a small presentation layer around existing Library and Catalog data: a stronger page header, visual summary cards, searchable recipe/model navigation, and more legible identity/action sections. Keep detail and authoring workflows separate so the Library remains the operational home and Catalog remains the advanced authoring surface.

**Tech Stack:** React 19, TypeScript, Vite, Vitest, Testing Library, CSS custom properties.

## Global Constraints

- Do not add a second catalog or recipe authority.
- Do not change API contracts, route semantics, or operation behavior.
- Keep exact model-version, harness, runtime-distribution, topology, digest, and placement evidence visible.
- Keep keyboard navigation, focus management, minimum 44px targets, and responsive mobile navigation.
- Reuse existing colors and tokens; do not add a UI framework or runtime dependency.
- Verify with focused Vitest tests, the full web test suite, TypeScript build, and Vite production build.

### Task 1: Add visual Library summary and navigation affordances

**Files:**
- Modify: `control/web/src/pages/library.tsx`
- Modify: `control/web/src/components/library-browser.tsx`
- Modify: `control/web/src/pages/library.css`
- Test: `control/web/src/pages/library.test.tsx`

**Interfaces:**
- Consumes: existing `LibrarySnapshot`, `LibraryRoute`, and `LibraryRecipeSummary` values.
- Produces: accessible summary metrics and client-side filtering without changing the server snapshot or route model.

- [ ] **Step 1: Write failing tests** for a Library overview summary, search input, filtered model/recipe rows, and a clear-search action.
- [ ] **Step 2: Run the focused Library tests** and confirm the new assertions fail.
- [ ] **Step 3: Implement summary metrics** for model versions, recipes, linked/unlinked recipes, and visible operational context; add a labeled search field that filters the already loaded bounded window only.
- [ ] **Step 4: Add model/recipe card hierarchy** with recognizable type labels, status/origin treatment, and a clear selected state while retaining existing links and ARIA state.
- [ ] **Step 5: Run focused Library tests** and confirm filtering, clear behavior, pagination notice, and route navigation remain correct.
- [ ] **Step 6: Commit** with `git commit -m "feat: polish library navigation"`.

### Task 2: Make recipe authority read as a product detail view

**Files:**
- Modify: `control/web/src/components/library-recipe-detail.tsx`
- Modify: `control/web/src/components/library-recipe-visual.tsx`
- Modify: `control/web/src/components/library-recipe-detail.css`
- Modify: `control/web/src/pages/library.css`
- Test: `control/web/src/pages/library.test.tsx`

**Interfaces:**
- Consumes: the existing immutable `LibraryRecipeDetail` and operation action callbacks.
- Produces: a visual identity strip, compact authority facts, and clearer lifecycle/action grouping with the same buttons and callbacks.

- [ ] **Step 1: Write failing tests** for the identity strip, exact digest labels, grouped lifecycle state, and preserved advanced workflow links.
- [ ] **Step 2: Run the focused tests** and confirm the new semantic labels fail.
- [ ] **Step 3: Implement the detail hierarchy**: title/description, immutable status, exact model/harness/runtime identities, topology/resource summary, and action grouping.
- [ ] **Step 4: Add responsive CSS** for narrow screens, long digests, operation rows, and readable visual-document sections.
- [ ] **Step 5: Run focused tests** and confirm existing placement/action assertions still pass.
- [ ] **Step 6: Commit** with `git commit -m "feat: refine recipe authority detail"`.

### Task 3: Bring Catalog authoring and cards into the same visual system

**Files:**
- Modify: `control/web/src/pages/catalog.tsx`
- Modify: `control/web/src/pages/recipe-editor.tsx`
- Modify: `control/web/src/components/recipe-summary.tsx`
- Modify: `control/web/src/styles.css`
- Test: `control/web/src/pages/catalog.test.tsx`
- Test: `control/web/src/pages/recipe-editor.test.tsx`

**Interfaces:**
- Consumes: existing Catalog API methods and typed recipe summaries.
- Produces: clearer catalog header/actions, recipe cards with exact identity hierarchy, and visually grouped authoring steps without changing submission payloads.

- [ ] **Step 1: Write failing tests** for the catalog hero, origin/status badges, identity metadata, and preserved create/import actions.
- [ ] **Step 2: Run focused Catalog/editor tests** and confirm the new assertions fail.
- [ ] **Step 3: Implement the shared visual treatment** for catalog cards, import review, editor sections, and success/error notices.
- [ ] **Step 4: Run focused Catalog/editor tests** and confirm existing import and save flows pass.
- [ ] **Step 5: Commit** with `git commit -m "feat: align catalog visual language"`.

### Task 4: Verify the complete web surface

**Files:**
- Modify: `control/web/src/styles.css` only if cross-page regressions are found.
- Modify: `control/web/src/pages/library.css` only if responsive regressions are found.

**Interfaces:**
- Consumes: completed Library and Catalog presentation changes.
- Produces: verified web assets suitable for the existing control image build.

- [ ] **Step 1: Run all web unit/component tests** with `npm test -- --run` from `control/web`.
- [ ] **Step 2: Run TypeScript and Vite build** with `npm run build` from `control/web`.
- [ ] **Step 3: Run `git diff --check` and inspect the final diff** for accidental API, authority, or generated-client changes.
- [ ] **Step 4: Commit any final verification-only fixes** with `git commit -m "chore: verify polished control web"`.

