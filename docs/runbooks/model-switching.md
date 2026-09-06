# Recipe activation and model changes

Vonk Forge does not switch named profiles. A model family can have many
immutable model versions, and each version can have multiple recipes for a
specific execution harness, runtime distribution, patch bundle, and topology.
The recipe revision is the unit the controller resolves and activates.

For the complete clean-fleet acceptance path, follow the [development agent
workload acceptance runbook](development-agent-workloads.md) after the recipe
has passed its source and placement gates.

## Inspect current state

Open the private browser and choose `Library`. It groups recipes by exact model
version and shows the accepted revision, cluster placement, node freshness,
runtime state, and available actions. A model-family label is for navigation;
the content-addressed model-version and recipe revision remain authoritative.

## Select a reviewed recipe

Use `Library` to select an immutable recipe revision from the managed catalog.
The library shows the model version, execution harness, runtime distribution,
topology, artifact identities, endpoint aliases, and resource envelope.

The lifecycle is deliberately explicit:

1. Review the immutable recipe and its provenance.
2. Confirm the source security/build evidence supplied by the catalog.
3. Map the revision to one or more compatible Spark nodes.
4. Stay in Library and preview the install/load action before applying it.

The control API performs identity, capacity, placement, evidence, and route
checks. The browser never invents a digest or bypasses those checks.

## One Spark, two Sparks, or many

Single-node recipes run one rank on one enrolled Spark. Distributed recipes
declare a tensor-parallel or other gang topology and list every expected rank.
The same recipe contract works for one, two, or many Sparks; placement and
readiness determine whether a specific fleet can run it. A recipe may be
accepted for one topology without being accepted for another.

## Stop, uninstall, and rollback

Use the action preview in Library. Stop withdraws the route before stopping the
run. Uninstall removes the selected recipe installation after its preview and
does not delete unrelated model artifacts. To roll back, select an earlier
accepted immutable revision and follow the same preview, evidence, mapping,
and activation gates.
