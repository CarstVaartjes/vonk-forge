# Recipe library packages

Controllers read signed GitHub releases of the recipe repository
(see [the recipe library](../operators/recipe-library.md#development-versus-production)).
Release assets are always downloaded through the Controller's in-project
Caddy relay (`http://caddy:8085`, built in); `VONK_RECIPE_LIBRARY_RELEASE` is
the only operator setting and optionally holds an exact release. There is no
unsigned package source.

Each release carries one asset, `recipe-library.tar`: `SHA256SUMS`, its
Sigstore bundle `SHA256SUMS.sigstore.json`, the generated schema-2
`catalog-index.json`, and one package per recipe, all under the flat names
`SHA256SUMS` lists. The Controller downloads it only when the release listing
reports a new bundle digest, trusts it only after `SHA256SUMS` verifies against
the pinned publisher workflow identity, and then checks the index and every
package once against the digests it lists. The index must be built from the
signed commit, and each recipe entry names its package as `packages/<asset>`
together with the package `sha256`, `expected_bytes`, and media type
`application/vnd.vonk-forge.recipe-package.v2+tar+gzip`.

Each package is a deterministic gzip-compressed tar archive containing
`manifest.json`, `recipe.json`, the exact `models/<slug>.json` Model
documents it references, and its build context and fixtures. The
manifest pins every member's SHA-256 and byte size and pins the publisher, slug,
and recipe content digest. The package does not contain the repository commit,
so unrelated repository changes leave its digest unchanged. Weights and OCI
images are never included.

Verified packages are stored by digest in the persistent
`state/recipe-library-packages` cache when the bundle is ingested (a digest
already stored is not rewritten); the reader then checks member identities and
rejects unsafe tar members or oversized archives. A package absent from the
bundle or with other bytes skips only its recipe as a
`recipe_package.release_incomplete` problem. A
package reader's `prepare` hook validates all packages in a candidate release
before the managed catalog sync writes its first revision or link. An
integrity or transport failure rejects the whole candidate. A model or recipe
document that is intact but does not validate against this Controller's
contract is skipped on its own and reported as a
`recipe_package.document_incompatible` problem; the rest of the release still
applies. Package cache files are written with an fsync and atomic rename, and
the release signature material is kept beside the cached index, so a restart
re-verifies and imports the previous generation offline. A failed candidate is
recorded as a failed sync run and never becomes the active catalog. The
sync runs by itself: again after a Controller upgrade (even for the same
commit), on every interval while the last run left problems, and a run that
makes no progress for ten minutes is replaced. The
`GET /api/catalog/managed-recipes/sync-status` route reports it. When the automatic sync cannot read the release at all,
sync-status still reports the last applied run and adds `last_error` (code,
detail, `occurred_at`) until a later sync succeeds; the sync retries after 30
seconds, doubling up to the sync interval.
