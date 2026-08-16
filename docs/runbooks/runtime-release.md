# Recipe runtime publication

Runtime images and recipe source bundles are published by the repository’s
GitHub Actions workflows. Operators do not copy model adapters to Sparks with a
developer-machine SSH script.

## Publication boundary

Every recipe selects an exact model-version identity, execution harness,
runtime distribution, optional patch bundle, and topology. A successful build
produces content-addressed source/build evidence and an immutable image or
artifact identity. Development and production channels are selected by the
trusted publication workflow; mutable convenience tags never replace the
digest recorded in a resolved recipe revision.

## Operator flow

1. Maintain the draft in `Catalog`.
2. Resolve the revision and run the source/build gate.
3. Attach the actual local or CI test report.
4. Select a Spark builder with enough memory and disk.
5. Publish only through the approved GitHub Actions workflow.
6. Map and activate the exact revision from `Library`.

The control service verifies source policy, image identity, artifact receipts,
node compatibility, and route readiness. Runtime secrets are projected from
the NAS secret files and are never baked into a published image or source
bundle. See [the supply-chain runbook](supply-chain.md) and [the model catalog](../operators/model-catalog.md)
for the detailed gates.
