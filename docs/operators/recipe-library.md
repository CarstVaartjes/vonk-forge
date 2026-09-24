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

The latest durable result is available from
`GET /api/catalog/managed-recipes/sync-status`. An explicit refresh uses
`POST /api/catalog/managed-recipes/sync` with a fresh UUID `request_key` and,
when the caller already reviewed a snapshot, its 40-character
`expected_commit`. Reusing a request key with different semantics or racing a
second sync fails closed.

The recipe library's GitHub Actions workflow calls the reusable validator in
this repository. Before publishing a production recipe-library release, pin
the validator to an exact `vonk-forge` commit or release tag. Publication is
GitHub Actions-only and has no access to runtime secrets.

## Validate a checkout locally

From the exact platform task worktree and an owned recipe-library checkout:

```bash
export VONK_PLATFORM_ROOT="$PWD"
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes

cd "$VONK_RECIPE_LIBRARY_ROOT"
tools/build-catalog-index
tools/build-catalog-index --check
"$VONK_PLATFORM_ROOT/control/.venv/bin/python" \
  "$VONK_PLATFORM_ROOT/scripts/validate-recipe-library" \
  --library-root "$VONK_RECIPE_LIBRARY_ROOT" \
  --platform-root "$VONK_PLATFORM_ROOT" \
  --json
cd "$VONK_PLATFORM_ROOT"
```

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

The Controller is the only import path. It resolves the configured library
branch to one immutable commit, validates the package index and dependency
closure, and records the durable result through
`POST /api/catalog/managed-recipes/sync`. Use the matching
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
repository-scoped `GET` requests, remove credentials, and relay the commit
lookup to `api.github.com` and immutable catalog/package paths to
`raw.githubusercontent.com`. The NAS therefore needs ordinary outbound HTTPS
and DNS access, but it never needs a GitHub token.

## Custom libraries

Operators may maintain a private or forked recipe library. It must pass the
same independent validator and use schema-2 Model/Recipe documents with one
self-contained package per recipe. A custom package cannot replace a harness
implementation or weaken the runtime security and evidence contract.

### Distributed cold-start budget

Set `VONK_DISTRIBUTED_START_TIMEOUT_SECONDS` on both the Controller API and
worker to change the budget a distributed model gets to load weights or compile
kernels. Values must be between 60 and 3600 seconds, and the default is 3600 —
a bounded 60-minute cold start. The shipped Compose default is the same 3600, so
a deployment that sets nothing still admits a long cold start; a test asserts
those two defaults agree rather than repeating the number. This sets one
immutable deadline when the run is admitted, covering rank launch and
collective readiness.
Changing the setting affects newly admitted runs only. It does not extend active
deadlines, publish an unready endpoint, or change rank-loss recovery and stop
timeouts. Native agents continue enforcing the signed operation's deadline.
The default is deliberately the largest value the contract allows: 60 minutes is
headroom for a frontier model that must load weights and compile kernels across
two Sparks, and the lease-lapse evidence now makes a start that stalls visible
long before the budget ends. It is not a promise that every model needs that
long, and it is not evidence that 60 minutes is enough — if a real start
exhausts it, that is the number to raise the ceiling for, rather than a guess
made now. Normal
`vonkctl profile load` progress displays the effective initial-start budget,
deadline, and current load/JIT phase when the Run/Switch child reaches startup.
Rank-loss recovery and final route publication keep their separate deadlines.
