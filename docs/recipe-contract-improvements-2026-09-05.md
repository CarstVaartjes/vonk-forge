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

## 2. Image or build, with one reusable Controller cache

A Recipe either selects an executable image or declares how to build one.
Image-only recipes need no dummy build context. Prepare missing images once and
reuse the resulting Controller artifact across the assigned Sparks. Run and
Switch obtain missing assets automatically and communicate actual progress.
Changing a setting that only needs a restart must not rebuild an unchanged image.

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
Distributed assignments account for the whole Spark group.

## 5. Test representative serving behavior

Maintainer checks exercise the declared interface: HTTP serving for endpoint
models and typed input/output jobs for image, audio, video or other workloads.
Check useful output properties without brittle exact model answers. Cover
restart and cache reuse; lightweight readiness and physical model acceptance
remain distinct from structural catalog validation. A recipe in the repository
should work without adding a user-facing qualification ceremony.
