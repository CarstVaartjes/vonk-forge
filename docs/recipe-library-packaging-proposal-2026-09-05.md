# Self-contained recipe packages and incremental sync

Status: approved for implementation by user on 2026-09-05. This replaces the earlier full-library archive proposal and the subsequent shared-package suggestion.

## Product decision

Keep one small catalog index and one self-contained immutable package per recipe. Controller compares package hashes with persistent local cache and downloads only missing/new/changed packages. There is no shared-package dependency graph for Controller to resolve. Forking a recipe creates an independent package. Model weights and container images remain separate digest-cached artifacts and are never bundled into these small recipe packages.

Repository sources may remain modular and share authoring files. The packager copies the complete needed closure into each recipe package. Duplicating small Dockerfiles/scripts/patches/metadata is deliberate and preferable to runtime dependency resolution. Shared authoring changes rebuild affected packages; unaffected package bytes/hashes remain stable. A recipe package is sufficient for local catalog import and build planning without fetching GitHub source blobs or another recipe package.

## Publication format

Small index: supported schema 2 contract with publisher/repository, exact source commit/catalog generation and sorted recipe entries. Each entry binds recipe identity/revision, package digest, compressed size, immutable download location and minimum supported consumer contract. Choose exact names with existing catalog-index tooling; do not introduce competing authorities or schema-1 fallback paths.

Each package contains a manifest, recipe document, exact referenced model/runtime/harness metadata needed for validation and execution planning, and complete build sources including Dockerfile, patches and scripts. Preserve original content identities for existing entities/source bundles. Self-contained metadata does not mean embedding model payloads, runtime image layers or vendoring upstream binary dependencies. Package manifest binds file paths/sizes/digests and its recipe identity. Deterministic archive generation normalizes order/timestamps/metadata; same inputs yield same bytes. A repository commit changing unrelated recipes must not alone change every package hash: record whole-catalog commit in the index and use recipe-relevant content identity for package bytes.

Publish immutable package assets and a trusted small index using existing release/static hosting mechanisms. Avoid GitHub per-file blob/contents API reads during normal hydration. Preserve existing publisher verification/provenance; a checksum from an unauthenticated authority is not standalone authenticity. Publication must upload and verify all referenced package assets before promoting the index. No live publication or deployment is implied by implementation authorization.

## Controller synchronization

1. Fetch/check index conditionally and validate publisher/schema/limits.
2. Compare expected package digests against verified persistent local cache. Unchanged verified packages require no network transfer even after restart.
3. Fetch missing packages with bounded concurrency, timeouts and retry/backoff; store immutable verified bytes. Do not redownload one package per dependent profile or shared model.
4. Validate each package identity and complete closure locally. Stage the complete candidate catalog generation; apply atomically or switch the active-generation pointer only after validation succeeds. A large generation can stage incrementally without exposing partial changes.
5. Retain the previous active catalog on failure. Keep historical recipe revisions referenced by profiles/runs. Withdrawals are evaluated from the complete verified index, never partial download results. Garbage collection cannot delete referenced packages.

First sync downloads all advertised recipe packages; subsequent updates fetch only changed/new/missing ones. An invalid/missing package prevents promotion of that candidate generation, but completed downloads remain reusable on retry and previous recipes remain usable. Manual/offline import uses the identical package validation, with explicit scope for importing one recipe versus an entire index generation; it must not withdraw unrelated recipes.

## Interface

Automatic Library update is background work: 'Updating Library · 3 of 5 recipes downloaded', then success or 'Update failed; using previous Library' with retry and details. This is not model download progress and must not imply weights/images are being fetched. No dependency-package choices, readiness ceremony or mandatory review. Normal Run/Switch continues using local recipe revisions during hosting outages.

## Work ownership and verification

Sol assigns Luna owners for recipe repo packager/publication tooling and Controller importer/sync, using isolated worktrees. Agree index/package fixtures before parallel implementation. Coordinate with existing cache, recipe_library and catalog_sync edits. This authorizes packaging/tooling/consumer changes; broad recipe behavior rewrites and live recipe qualification remain separate.

Required tests use the real packager and HTTP/local import consumer: initial multi-recipe sync; one-recipe edit fetches only that package; shared authoring change alters only actual dependents; a fork imports without original package; restart reuses cache; missing/corrupt package never promotes a partial catalog; retry reuses completed downloads; offline retained library works; pinned histories survive updates/withdrawals; malformed archive paths, symlinks, duplicate entries and oversized/decompression payloads reject safely; package identity binds recipe/source metadata. Test published fixture shape and asset availability ordering rather than merely asserting workflow text.

Record exact commits and focused checks. Do not claim quota outages eliminated; this removes request amplification and preserves local continuity.

## Explicit cross-repository conversion scope

User confirmed this includes conversion of the existing vonk-forge-recipes repository, not merely a format for future additions, plus the Controller's actual download path. Inventory every current recipe at the exact latest recipe-repository origin/main, preserve its behavior and content references, and produce a complete self-contained package for each. Update existing catalog generation/validation/publication tooling to emit the agreed index/packages. Shared authoring files can remain; complete closure must be copied into every dependent package. Report any recipe that cannot be converted rather than silently omitting it.

Controller's normal managed sync must consume those packages, persist verified downloads and fetch only changed/new/missing package hashes. A disconnected optional importer is insufficient. Coordinate a recipe-repo Luna owner and platform consumer Luna owner with common schema and fixture contract. Cross-repository acceptance builds the full existing catalog, serves its actual output to Controller, proves initial import and changed-only subsequent download, and checks fork independence, offline restart and failed-update continuity. Preserve existing runtime behavior; broader recipe tuning and physical qualification remain separate. No live publication/deployment occurs merely because repository conversion is authorized.
