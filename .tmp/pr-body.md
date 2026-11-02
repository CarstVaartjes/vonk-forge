## F1b: the Controller consumes public contracts 3.0.0

Display-split switch-over, Controller side (design delta 2026-11-01 section 5 step 2). Paired with vonk-forge-recipes draft PR #1987 (`feat/display-split-r3`, contracts 3.0.0). The recipes and contracts pin is the exact commit `cb4fcdf020d6d4c56a74851c8c886543ae420d57` (wheel built from it); it is re-pinned to the merge commit `R` of #1987 before this lands.

## Principles

- **Identity bytes of running work.** Exact-id reads are unchanged. Stored 2.x revisions keep the bytes and digests they were written with; nothing rewrites a stored revision digest, so the Spark agents' persisted per-installation digests stay valid (uninstall keeps working). The first v3 sync creates one new head revision per recipe and Model; that is the accepted one-time "Update available" (nothing restarts or reinstalls by itself, loads not yet started re-plan). Files are addressed by file sha plus format (`artifact_key`), images by build key: no re-download, no rebuild. Observed run memory keyed by content digest resets once (accepted; no re-keying logic added).
- **Deploy order.** Recipes first (R3 merges, publishes v3.0.0), then this Controller, deployed at once after merge. The release gate already follows the installed `CONTRACT_MAJOR`, so a 2.x Controller keeps v2.8.x and this one reads v3.x.
- **One contract, one path.** Stored 2.x rows are read through the single existing lenient reader (`read_recipe` / `read_model` drop unknown fields); no second reader, no legacy shape declared. Writes refuse the retired display keys with one named function and one path set.
- **No double logic.** `rederive_projection` is the one repair rule for a stored projection (document fields from the document, each sync-recorded field from the stored projection key by key). Both read-time repair sites and the entity repair call it.

## Changes

- Pins: `control/pyproject.toml`, `uv.lock`, `public-contracts.lock`, the 3.0.0 wheel, `scripts/verify-supply-chain` and the Dockerfile, the acceptance pin file and `profile-effect-consumer-proof.yml`.
- Projections drop `metadata` (both), `title`, `description`, `tags` (recipe); `recipe_document_projection` and a new `model_document_projection` stop writing them.
- `catalog_projection_reconcile`: once per start, in the background, in id batches (skip-locked), writes the re-derived projection only when it differs; an unreadable row is logged and skipped; it does not gate serving.
- `_parse` refuses a document carrying `metadata` or `identity.*.title` (`DOCUMENT_INVALID`); newer-minor fields stay tolerated. `create_draft(document, display, *, actor)`: display required, identity-checked, root title from the display. Import no longer reads titles from documents (the sync's display pass owns them).
- Package decode takes title, description and tags from the index item (its display), not the package's `recipe.json`.
- `RecipeRevisionView` loses `title` / `description` (no source of truth in the document any more).
- Tests and fixtures move to 3.0 documents (`reword` replaces display edits as the "same recipe, edited" mutation); launch-fixture corpus refreshed from the 3.0 index (28 recipes, same set except the two removed from the library); web fixture reads the corpus from `library-corpus.json` plus per-slug displays.
- Acceptance canary: `FixtureReader` takes each entry's `display` from the index instead of deriving it; the `_installed_display` bridge for releases that predate display (#1949) is removed because every promoted release now carries display. `_editorial_successor`, siblings and the job canary edit `provenance.attribution` instead of metadata.

## Tests

New `test_catalog_display_split.py`: a real 2.x-shaped row (document with `metadata`, projection with `metadata/title/description/tags` plus `package_handle` and `prebuilt_image`) reads under the 3.0 types, is persisted once by the reconcile (second pass writes nothing), keeps the sync-recorded state and leaves digests, build keys and document bytes untouched; retired keys are refused by `create_draft`, `revise` and both imports; a newer-minor field is accepted; the shared document vectors agree with the Controller. New removal test (Postgres): a removal review by the new Model digest finds the cache set bound under the old 2.x digest through the publisher/slug lineage, with the same `artifact_key`, and removes it with no download.

## Known item for review: upgrade-carry across the switch release

The carry lane imports the canary into the previous promoted release's Controller before upgrading. For this one release the previous Controller is a 2.x Controller, which cannot parse a 3.0 canary document (`metadata` is required in 2.x). The harness no longer bridges that (it only bridged pre-display releases); the lane for this single release needs the owner's decision (run the pre-upgrade phase against the previous library revision, or skip carry once).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
