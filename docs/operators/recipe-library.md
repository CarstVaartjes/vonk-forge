# Standard recipe library

The standard public recipe library is the separate
[`vonk-forge-recipes`](https://github.com/CarstVaartjes/vonk-forge-recipes)
repository. This platform repository owns the execution contract and control
plane; the recipe repository owns the reviewed model and recipe material.

## Authority split

| Concern | Authority |
| --- | --- |
| JSON schemas, harness compilers, admission, installation, and Spark acceptance | `vonk-forge` at an exact platform commit |
| Canonical `ModelDefinition` and `RecipeDefinition` documents | `vonk-forge-recipes` at an exact library commit |
| Catalog index and independent recipe packages | Signed `vonk-forge-recipes` GitHub release built from that commit |
| Installed state, active runs, routes, and local acceptance evidence | Local control-plane PostgreSQL |
| Weights, OCI layers, secrets, and fleet state | Never stored in the recipe repository |

The library is public because recipes are declarative metadata and build input.
That does not make every upstream model or dependency freely redistributable;
the operator still reviews the recorded license and access terms before
download or use.

## Contract compatibility

The `vonk_forge_contracts` package in the recipe repository is the single
source of truth for Model and Recipe documents; the Controller consumes the
exact commit pinned in `control/pyproject.toml`. The library's release version
is the contract version (`v2.0.0`), so new or changed models and recipes never
need a vonk-forge change: the recipe repository updates the existing release
for the current contract in place and records when its recipes last changed
(`updated_at` in `catalog-index.json`). An additive contract change (a new
optional field) is published as a new minor release (`v2.1.0`); a breaking one
as a new major release (`v3.0.0`) that needs a coordinated vonk-forge release.

The Controller reads published documents tolerantly, ignoring fields a newer
minor release added, and identifies each document by the `document_sha256` of
its published JSON as recorded in the signed index. A document it still cannot
read is skipped, listed under the sync-status `problems`, and the rest applies.

## Development versus production

The catalog index and recipe packages are not committed to the recipe
repository. Its `publish.yml` workflow builds them from the release commit and
attaches them to a GitHub release together with `SHA256SUMS` and
`SHA256SUMS.sigstore.json`, a keyless GitHub artifact attestation over
`SHA256SUMS`. The Controller follows the newest published release whose major
version is its own contract major (drafts are skipped); set
`VONK_RECIPE_LIBRARY_RELEASE` to an exact `v2.MINOR.PATCH` tag to hold one.
Sync status (`library_version`, `library_updated_at`), the Library page and
`vonkctl recipe library` show the release version and when its recipes last
changed. A release caught mid-update (assets being replaced) just fails that
sync; the previous verified catalog stays active and the next sync heals it.

Each sync verifies, before importing anything:

1. the Sigstore bundle offline against the Sigstore public-good trust root
   packaged at `control/src/vonk_control/resources/sigstore-trusted-root.json`;
2. that its certificate names the pinned publisher: identity
   `https://github.com/CarstVaartjes/vonk-forge-recipes/.github/workflows/publish.yml@refs/heads/main`,
   issuer `https://token.actions.githubusercontent.com`, source repository ID
   `1336002555` (never reused after a rename or deletion), ref
   `refs/heads/main` and a GitHub-hosted runner;
3. that the attestation's only subject is the downloaded `SHA256SUMS`, and
   that its provenance and certificate name the same source commit;
4. that `catalog-index.json` matches its `SHA256SUMS` digest and records that
   signed commit as `source_commit`;
5. that every package the index names is listed in `SHA256SUMS` with the
   index's own digest, and that downloaded package bytes match both.

An unsigned, wrongly signed, or inconsistent release fails the sync with a
`recipe_release.*` or `recipe_package.*` code and never replaces the imported
catalog. When GitHub is unreachable, the Controller reuses the previous
verified generation from its state directory, re-verifying the stored
signature. Automatic sync retries a failed attempt after 30 seconds, doubling
up to the sync interval. The packaged trust root is refreshed deliberately with
the pinned `sigstore` dependency when Sigstore rotates its keys.

Every imported recipe, dependency, source bundle, and receipt is verified
against the release's exact commit. The local controller resolves every recipe
dependency by `kind`, `publisher`, `slug`, and content digest; it never turns
a branch, display name, or `latest` tag directly into execution authority.

The Controller refreshes the managed catalog automatically every 15 minutes.
Opening Library also offers
**Update from Vonk Forge remote** for an immediate administrator-triggered
refresh. Both paths use the same durable, idempotent operation and exact commit
gate. The import receipt records the exact library commit and recipe path;
re-importing the same recipe digest is idempotent. The checkout is never mounted
into a running workload.

Managed synchronization only owns recipes whose source is
`recipe_library`. A slug collision is reported as a conflict for operator
review. If a managed recipe disappears remotely, its immutable local history
and any installation remain intact. It is surfaced as withdrawn only while it
is installed or running; synchronization never stops or uninstalls it.
When a newer revision is imported, existing installations and runs remain bound
to their old immutable revision and are reported as stale until the operator
reviews an update.

Library lists only the current accepted revision of each recipe, selected by
the Controller's catalog head. Recipe links and counts on Models use that same
current set. Cached older revisions do not become extra Library choices;
their exact records remain available to the installations and runs that use
them. A pending or failed update never replaces the accepted recipe.

The Controller syncs the catalog automatically; there is no manual trigger. It
syncs again after its own upgrade, even for the same commit, and retries while
the last run left problems. The latest durable result is available from
`GET /api/catalog/managed-recipes/sync-status`.

The recipe library's GitHub Actions workflow calls the reusable validator in
this repository. Before publishing a production recipe-library release, pin
the validator to an exact `vonk-forge` commit or release tag. Publication is
GitHub Actions-only and has no access to runtime secrets.

## Validate a checkout locally

From the exact platform task worktree and an owned recipe-library checkout:

```bash
export VONK_PLATFORM_ROOT="$PWD"
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes

scripts/build-recipe-library "$VONK_RECIPE_LIBRARY_ROOT"
"$VONK_PLATFORM_ROOT/control/.venv/bin/python" \
  "$VONK_PLATFORM_ROOT/scripts/validate-recipe-library" \
  --library-root "$VONK_RECIPE_LIBRARY_ROOT" \
  --platform-root "$VONK_PLATFORM_ROOT" \
  --json
```

`scripts/build-recipe-library` runs the recipe checkout's own
`tools/build-catalog-index`, which writes the ignored `catalog-index.json`,
`qualification/qualification-index.json` and `packages/` build outputs in
place. Platform CI, the reusable recipe validator, `scripts/qualify-recipe`,
the campaign runner and the tests read those outputs; run the command again
after changing the checkout.

Set the library path to its owning task worktree when recipe edits are in
progress; do not regenerate over another task's uncommitted library files.
Sync the platform worktree's control environment using the
[testing policy](../testing-and-ci.md#lint-format-type-and-generation-checks).
Record both exact commits and the recipe content digest with the result.

To run structural qualification for one recipe from the external checkout:

```bash
./scripts/qualify-recipe \
  --recipe "$VONK_RECIPE_LIBRARY_ROOT/recipes/deepseek-v4-flash-0731-ds4-single.json" \
  --library-root "$VONK_RECIPE_LIBRARY_ROOT" \
  --platform-root . \
  --level structural
```

Structural qualification resolves the selected `RecipeDefinition` and its
immutable package. It verifies the exact Model snapshots and selected files,
package manifest and source/job closure, pinned direct-image or source-build
inputs, and the current runtime compiler projection for engine arguments,
topology, interface, serving checks, writable paths, and security. The
independent library validator is run against the same checkout and reports
dynamic catalog counts; no model weights or upstream sources are fetched.
Structural output is repository evidence only. Container qualification remains
an environment-dependent native `linux/arm64` gate, and Spark acceptance
requires the designated physical lane.

The Controller is the only import path. It resolves the configured signed
release, validates the package index and dependency closure, and records the
durable result. Use the
`GET /api/catalog/managed-recipes/sync-status` response to inspect the
commit, counts, conflicts, and withdrawn revisions. There is no platform-local
Model or Recipe ledger to edit or import around the Controller.

Container and Spark qualification still require the native ARM64/NVIDIA
environment and exact artifact cache described in the acceptance runbook.

## Runtime argument authority

The canonical recipe owns the ordered engine arguments and settings. The
platform compiler preserves those values, adds only platform-owned topology and
interface arguments, and rejects attempts to control mounts, users, networks,
capabilities, or other security-owned fields. Unknown engine options remain
representable so the pinned runtime can report its own error.

The controller still rejects reserved `VONK_*` names, dynamic-loader variables,
interpreter injection hooks, and executable-path overrides. Values remain
bounded recipe scalars; this capability does not grant secret access or shell
execution.

In the production Compose topology the control API retains no general outbound
network. Its GitHub client uses internal Caddy listeners that accept only
repository-scoped `GET` requests and remove credentials: `:8083` relays the
release lookup to `api.github.com` (one unauthenticated REST call per sync),
and `:8085` relays release asset downloads to `github.com` and GitHub's release
asset origin, `release-assets.githubusercontent.com`, scoped to the recipe
repository's numeric ID. The NAS therefore needs ordinary outbound HTTPS and
DNS access, but it never needs a GitHub token.

## Custom libraries

Operators may maintain a private or forked recipe library. It must pass the
same independent validator and use schema-2 Model/Recipe documents with one
self-contained package per recipe. A custom package cannot replace a harness
implementation or weaken the runtime security and evidence contract.

### Distributed cold-start budget

A distributed model gets a fixed 60-minute (3600 s) budget to load weights or
compile kernels. It is one immutable deadline set when the run is admitted,
covering rank launch and collective readiness. It does not extend active
deadlines, publish an unready endpoint, or change rank-loss recovery and stop
timeouts. Native agents continue enforcing the signed operation's deadline.
The budget is deliberately the largest value the contract allows: 60 minutes is
headroom for a frontier model that must load weights and compile kernels across
two Sparks, and the lease-lapse evidence now makes a start that stalls visible
long before the budget ends. It is not a promise that every model needs that
long, and it is not evidence that 60 minutes is enough — if a real start
exhausts it, that is the number to raise the ceiling for, rather than a guess
made now. Normal
`vonkctl profile load` progress displays the effective initial-start budget,
deadline, and current load/JIT phase when the Run/Switch child reaches startup.
Rank-loss recovery and final route publication keep their separate deadlines.
