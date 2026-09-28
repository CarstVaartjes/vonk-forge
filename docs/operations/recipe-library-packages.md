# Recipe library package channel

Production Controllers read signed GitHub releases of the recipe repository
(see [the recipe library](../operators/recipe-library.md#development-versus-production)).
Release assets are always downloaded through the Controller's in-project
Caddy relay (`http://caddy:8085`, built in); `VONK_RECIPE_LIBRARY_RELEASE` is
the only operator setting and optionally holds an exact release. The unsigned
package channel below exists only for the acceptance lifecycle canary, which
sets `VONK_RECIPE_LIBRARY_PACKAGE_URL` directly on its disposable Controller;
it is not part of the Compose configuration and carries no release signature.

The platform publisher emits `index.json` and one immutable package for each
recipe. The package channel is served at the configured origin with these
routes:

* `GET /v1/recipe-library/index.json` returns the generated schema-2
  `catalog-index.json` descriptor with `kind: recipe-library-index`, the
  repository, the exact source commit, and one entry per recipe package. A static
  deployment maps the publisher's `catalog-index.json` and package directory to
  these routes.
* Each recipe package entry contains `source_path`, `content_sha256`, and a
  package descriptor with its `sha256`, byte `expected_bytes`, media type, and
  relative `path`.
* `GET /v1/recipe-library/recipe-packages/<publisher>--<slug>.tar.gz` returns
  the package with media type
  `application/vnd.vonk-forge.recipe-package.v2+tar+gzip`.

Each package is a deterministic gzip-compressed tar archive containing
`manifest.json`, `recipe.json`, `recipe-release.json`, complete authoritative
`metadata/...` catalog documents, and the `source/...` build closure. The
manifest pins every member's SHA-256 and byte size and pins the publisher, slug,
and recipe content digest. The package does not contain the repository commit,
so unrelated repository changes leave its digest unchanged. Weights and OCI
images are never included.

The Controller validates the index over its configured HTTPS origin, fetches a
package only when its digest is absent or changed in the persistent
`state/recipe-library-packages` cache, verifies the package digest and member
identities, and rejects unsafe tar members or oversized archives. A package
reader's `prepare` hook validates all packages in a candidate index before the
managed catalog sync writes its first revision or link. An integrity or
transport failure rejects the whole candidate. A model or recipe document that
is intact but does not validate against this Controller's contract is skipped
on its own and reported as a `recipe_package.document_incompatible` problem;
the rest of the release still applies. Package cache files are
written with an fsync and atomic rename, so a restart can import the exact
same package offline. Once every package has been applied, the package-backed
sync publishes links and missing-recipe reconciliation in one database
transaction. A later apply or publish failure therefore leaves the previous
active generation visible; partial package candidates are recorded as failed
sync runs and never become the active catalog. A single offline package import
uses the normal exact-recipe import path and does not reconcile the rest of the
managed library. The existing `/api/catalog/managed-recipes/sync` and
`/api/catalog/managed-recipes/sync-status` routes remain the Controller's
authenticated sync API. When the automatic sync cannot read the release at all,
sync-status still reports the last applied run and adds `last_error` (code,
detail, `occurred_at`) until a later sync succeeds; the sync retries after 30
seconds, doubling up to the sync interval.
