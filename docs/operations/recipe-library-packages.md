# Recipe library package channel

The platform publisher emits `index.json` and one immutable package for each
recipe. The package channel is served at the configured origin with these
routes:

* `GET /v1/recipe-library/index.json` returns the generated schema-2
  `catalog-index.json` descriptor with `kind: recipe-library-index`, the
  repository, the exact source commit, and 84 recipe package entries. A static
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
managed catalog sync writes its first revision or link. Package cache files are
written with an fsync and atomic rename, so a restart can import the exact
same package offline. Once every package has been applied, the package-backed
sync publishes links and missing-recipe reconciliation in one database
transaction. A later apply or publish failure therefore leaves the previous
active generation visible; partial package candidates are recorded as failed
sync runs and never become the active catalog. A single offline package import
uses the normal exact-recipe import path and does not reconcile the rest of the
managed library. The existing `/api/v1/catalog/managed-recipes/sync` and
`/api/v1/catalog/managed-recipes/sync-status` routes remain the Controller's
authenticated sync API.
