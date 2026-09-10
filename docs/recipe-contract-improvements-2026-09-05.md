# Recipe contract design decisions

These five priorities were agreed on 5 September 2026. The accepted greenfield
and Pydantic decisions below supersede the early migration/package proposals.
For implemented contract ownership and validation, use [API contracts](api-contracts.md).
For remaining product gaps, use [the API review](audits/2026-09-07-api-goal-alignment.md).

## Current authority

There are exactly two public authoring documents: Model and Recipe. Their
canonical nested Pydantic definitions live in `vonk-forge-recipes/contracts/`.
Catalog tools and Controller ingestion use that package. New model families,
versions and creators are data, not new Python model classes. Platform operation,
progress, telemetry and Controller/Spark wire contracts remain platform-owned.

This is the first internal release, not a migration. Replace retired formats and
update all recipes, producers, consumers, fixtures and generated clients together.
Do not retain old parsers, duplicated definitions or fallback documents. Validate
persisted contract JSON as well as incoming API data. Follow the latest recipe
repository through the consumer update/build workflow and record the resolved
commit for reproducibility. A build record must not become a second editable
catalog authority.

## 1. One model-file authority

The Model document owns its sources and file manifest. A Recipe selects the
model and required files for its execution. Resolve that selection once and use
it for downloading, Controller/NAS caching and Spark distribution. Model download
is independent of a recipe. Do not duplicate editable model metadata in recipes
or manufacture a recipe choice for a model-only download.

## 2. Image or build, with one trusted Controller/NAS cache

A Recipe either selects an executable image or declares how to build one.
Image-only recipes need no dummy build context. Resolve the exact model file
manifest, recipe revision, runtime image identity and source/build inputs once.
`Prepare cache` explicitly downloads or builds the missing immutable model and
image assets in the trusted Controller/NAS cache, verifies them, and reports
durable progress. It does not revise a saved profile's identities.

Run, Switch and profile Apply require that exact cache state. If an asset is
missing or unverified, the operation is blocked with the named model or
recipe-image blocker and offers `prepare-cache`; it never fetches from an
upstream origin as a hidden prerequisite. Changing a setting that only needs a
restart must not rebuild an unchanged image.

After cache preparation, the Controller distributes the exact digest- and
size-bound assets to all selected Sparks in parallel. Each target verifies its
destination and image identity; a verified local copy is skipped. The
Controller then stops and safely replaces only conflicting workloads in the
requested scope, starts the desired workload, and reports durable per-Spark
transfer progress and serving readiness. Spark-local copies are derived
execution caches, never profile or cache authority.

## 3. Engines own their runtime invariants

The platform owns writable cache paths and engine launch conventions. Recipes
supply model/topology choices and engine options. Preserve unknown engine options
through the declared extension fields; engine metadata is not an exhaustive
allowlist. Enforce container isolation and writable-path rules at execution.
See [runtime writable paths](runtime-writable-path-contract.md).

## 4. Plan and launch use the same effective settings

Resolve settings once for launch, placement, memory planning and preparation
reuse. Distinguish current capacity from capacity released by planned stops.
Report missing resource evidence explicitly instead of inventing estimates.
Distributed assignments account for the whole Spark group. The apply plan
retains the exact canonical asset identities and target scope across retries;
completed distribution and replacement work is not discarded on a transient
failure. Readiness is observed per Spark and endpoint publication waits for the
required group state.

## 5. Test representative serving behavior

Maintainer checks exercise the declared interface: HTTP serving for endpoint
models and typed input/output jobs for image, audio, video or other workloads.
Check useful output properties without brittle exact model answers. Cover
restart, cache reuse, parallel distribution, verified-local-copy skipping,
safe workload replacement, and durable per-Spark progress/readiness. The
Controller/NAS cache may garbage-collect only local model objects that are no
longer referenced by a saved profile, active workload, or preparation
operation; it must preserve referenced objects. Managed NAS cache state is
trusted for ordinary profile reads and admission, so the contract must not
promise repeated full hashing of every profile asset.

Structural catalog and Controller/CI evidence, NAS preparation evidence,
Spark distribution evidence, and physical model/hardware acceptance are
separate gates. Lightweight readiness or a successful cache operation does not
prove physical Spark acceptance; that claim requires the designated Linux/
ARM64 or actual Spark lane and its retained bounded evidence. A recipe in the
repository should work without adding a user-facing qualification ceremony.
