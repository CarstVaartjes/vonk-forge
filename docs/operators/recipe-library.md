# Standard recipe library

The standard public recipe library is the separate
[`vonk-forge-recipes`](https://github.com/CarstVaartjes/vonk-forge-recipes)
repository. This platform repository owns the execution contract and control
plane; the recipe repository owns the reviewed model and recipe material.

## Authority split

| Concern | Authority |
| --- | --- |
| JSON schemas, harness compilers, admission, installation, and Spark acceptance | `vonk-forge` at an exact platform commit |
| Model groups, model versions, artifacts, runtime distributions, patches, recipes, and target ledger | `vonk-forge-recipes` at an exact library commit |
| Installed state, active runs, routes, and local acceptance evidence | Local control-plane PostgreSQL |
| Weights, OCI layers, secrets, and fleet state | Never stored in the recipe repository |

The library is public because recipes are declarative metadata and build input.
That does not make every upstream model or dependency freely redistributable;
the operator still reviews the recorded license and access terms before
download or use.

## Development versus production

Development follows the recipe library's `main` branch and is allowed to
change as pull requests merge. Production selects an approved immutable
recipe-library release tag and records its commit in the local import receipt. The local
controller resolves every recipe dependency by `kind`, `publisher`, `slug`,
and content digest; it never turns a branch, display name, or `latest` tag into
execution authority.

An administrator imports a validated checkout with the platform's
recipe-library import operation. The import receipt records
the exact library commit and recipe path; re-importing the same recipe digest
is idempotent. The checkout is never mounted into a running workload.

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

To preview the recipes that a fresh local control plane would receive:

```bash
./scripts/import-recipe-library \
  --library-root ../vonk-forge-recipes \
  --platform-root .
```

The preview imports only recipes whose target ledger status is `accepted`.
Use `--include-candidates` or an explicit `--recipe` only when deliberately
testing a candidate. Applying the plan additionally requires an administrator
token file and the control-plane URL:

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

The public Library API also fails closed when it projects qualification. It
returns the existing `cataloged` wire value (shown as **Accepted** in the UI)
only when the exact indexed recipe document explicitly carries the `accepted`
metadata tag. An explicit `candidate` tag, a missing declaration, or conflicting
`accepted` and `candidate` tags is shown as **Candidate**. This prevents missing
metadata from silently becoming an acceptance claim while preserving existing
API clients and filters.

In the production Compose topology the control API retains no general outbound
network. Its GitHub client uses an internal Caddy listener that accepts only
`GET` requests beneath the `CarstVaartjes/vonk-forge-recipes` API path, removes
credentials, and relays them over HTTPS to `api.github.com`. The NAS therefore
needs ordinary outbound HTTPS and DNS access, but it never needs a GitHub token.

## Custom libraries

Operators may maintain a private or forked recipe library. It must pass the
same validator and use the same v1 schemas. A custom recipe can add a source
bundle or patch bundle, but it cannot replace a harness implementation or
weaken the runtime security and evidence contract.
