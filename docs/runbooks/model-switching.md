# Profile activation and model changes

Profiles select exact model and recipe identities. The Controller/NAS cache is
the preparation authority: a profile choice is valid only while its complete
model artifact set and required runtime image are cached and verified. A
Spark-local copy can support current execution, but it is never used to make a
profile choice available or to repair the NAS cache.

For the complete clean-fleet acceptance path, follow the [development agent
workload acceptance runbook](development-agent-workloads.md) after the profile
has passed its cache and placement gates.

## Inspect current state

Open the private browser and choose `Library`. It groups profiles and exact
recipe choices by model version, and shows Controller/NAS cache state, cluster
placement, Spark runtime state, and available actions. A model-family label is
for navigation only; the exact cached model definition and recipe revision
remain authoritative.

## Select a cached profile choice

Use `Library` to select an immutable, fully cached model/recipe choice. The
library shows the model version, execution harness, runtime distribution,
topology, exact artifact identities, endpoint aliases, and resource envelope.
Profile creation and updates reject uncached choices. If an existing profile's
cache entry is missing, stale, or invalid, apply is blocked with a concrete
reason and offers **Prepare cache** for the affected model/image assets.

## Apply the profile

1. Choose a profile and its selected Sparks.
2. The Controller checks the exact cached assets and placement. Missing cache
   assets block apply; the result names each reason and the cache-preparation
   operation that can resolve it.
3. Once the cache is ready, the Controller distributes the exact model and
   image assets to all selected Sparks in parallel. It verifies destination
   bytes and image identity and skips any already verified local copy.
4. The Controller stops and replaces conflicting workloads, then reports
   starting and serving readiness per Spark. Progress is durable and identifies
   the affected target on partial failure.

Apply does not silently fetch from upstream, build on a Spark, or select a
different revision. Cache preparation is a separate operation. NAS garbage
collection may remove unused local model objects that are no longer referenced
by a saved profile, active workload, or preparation operation; Spark copies do
not pin or replace those references.

## One Spark, two Sparks, or many

Single-node recipes run one rank on one enrolled Spark. Distributed recipes
declare a tensor-parallel or other gang topology and list every expected rank.
The same profile contract works for one, two, or many Sparks. The Controller
distributes exact cached assets in parallel across the selected group, and
reports readiness for each target; placement, capacity, and topology still
determine whether that group can run it.

## Stop, replace, and recover

Apply stops and replaces only the conflicting workloads in the requested
profile scope. It preserves completed transfers and reports what is actually
running on each Spark if the operation is partial. Retry resumes the durable
operation and reuses verified cache and Spark-local copies.

To roll back, select an earlier profile or exact accepted recipe revision whose
required assets are cached, then apply it through the same cache, distribution,
replacement, and readiness path. If its cache is unavailable, prepare the cache
first; do not recover from a Spark-local copy.
