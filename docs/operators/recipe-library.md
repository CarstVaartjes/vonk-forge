# Standard recipe library

The standard public recipe library is the separate
[`vonk-forge-recipes`](https://github.com/CarstVaartjes/vonk-forge-recipes)
repository. This platform repository owns the execution contract and control
plane; the recipe repository owns the reviewed model and recipe material.

## Authority split

| Concern | Authority |
| --- | --- |
| JSON schemas, harness compilers, admission, installation, and Spark acceptance | `vonk-forge` at an exact platform commit |
| Canonical `ModelDefinition` and `RecipeDefinition` documents, index, and independent recipe packages | `vonk-forge-recipes` at an exact library commit |
| Installed state, active runs, routes, and local acceptance evidence | Local control-plane PostgreSQL |
| Weights, OCI layers, secrets, and fleet state | Never stored in the recipe repository |

The library is public because recipes are declarative metadata and build input.
That does not make every upstream model or dependency freely redistributable;
the operator still reviews the recorded license and access terms before
download or use.

## Development versus production

The Controller watches the recipe library's reviewed `main` branch, but each
sync first resolves that branch to one immutable Git commit. Every imported
recipe, dependency, source bundle, and receipt is then verified against that
exact snapshot. The local controller resolves every recipe dependency by
`kind`, `publisher`, `slug`, and content digest; it never turns a branch,
display name, or `latest` tag directly into execution authority.

The Controller refreshes the managed catalog automatically every 15 minutes by
default. Set `VONK_RECIPE_LIBRARY_SYNC_INTERVAL_SECONDS` between 60 and 86400
seconds only when a different cadence is required. Opening Library also offers
**Update from Vonk Forge remote** for an immediate administrator-triggered
refresh. Both paths use the same durable, idempotent operation and exact commit
gate. The import receipt records the exact library commit and recipe path;
re-importing the same recipe digest is idempotent. The checkout is never mounted
into a running workload.

Managed synchronization only owns recipes whose source is
`recipe_library`. Local drafts, WorkloadRun imports, forks, and other custom
recipes are never overwritten. A slug collision is reported as a conflict for
operator review. If a managed recipe disappears remotely, its immutable local
history and any installation remain intact. It is surfaced as withdrawn only
while it is installed or running; synchronization never stops or uninstalls it.
When a newer revision is imported, existing installations and runs remain bound
to their old immutable revision and are reported as stale until the operator
reviews an update.

The latest durable result is available from
`GET /api/v1/catalog/managed-recipes/sync-status`. An explicit refresh uses
`POST /api/v1/catalog/managed-recipes/sync` with a fresh UUID `request_key` and,
when the caller already reviewed a snapshot, its 40-character
`expected_commit`. Reusing a request key with different semantics or racing a
second sync fails closed.

The recipe library's GitHub Actions workflow calls the reusable validator in
this repository. Before publishing a production recipe-library release, pin
the validator to an exact `vonk-forge` commit or release tag. Publication is
GitHub Actions-only and has no access to runtime secrets.

## Validate a checkout locally

From a checkout of both repositories:

```bash
./scripts/validate-recipe-library \
  --library-root ../vonk-forge-recipes \
  --platform-root . \
  --json
```

To run structural qualification for one recipe from the external checkout:

```bash
./scripts/qualify-recipe \
  --recipe ../vonk-forge-recipes/recipes/deepseek-v4-flash-0731-ds4-single.json \
  --library-root ../vonk-forge-recipes \
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

To preview the recipes that a fresh local control plane would receive:

```bash
./scripts/import-recipe-library \
  --library-root ../vonk-forge-recipes \
  --platform-root .
```

Applying the plan additionally requires an administrator token file and the
control-plane URL:

```bash
./scripts/import-recipe-library \
  --library-root ../vonk-forge-recipes \
  --platform-root . \
  --control-url https://forge.example.test \
  --token-file .dev/admin-token \
  --apply
```

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
network. Its GitHub client uses an internal Caddy listener that accepts only
`GET` requests beneath the `CarstVaartjes/vonk-forge-recipes` API path, removes
credentials, and relays them over HTTPS to `api.github.com`. The NAS therefore
needs ordinary outbound HTTPS and DNS access, but it never needs a GitHub token.

## Custom libraries

Operators may maintain a private or forked recipe library. It must pass the
same independent validator and use schema-2 Model/Recipe documents with one
self-contained package per recipe. A custom package cannot replace a harness
implementation or weaken the runtime security and evidence contract.
