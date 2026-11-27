# Profile activation and model changes

Profiles select exact model and recipe identities. The Controller/NAS cache is
the preparation authority: a load prepares there the complete model artifact
set and runtime image each choice needs before it sends them to the Sparks. A
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

## Select a profile choice

Use `Library` to select an immutable model/recipe choice. The library shows the
model version, execution harness, runtime distribution, topology, exact
artifact identities, endpoint aliases, resource envelope, and cache state. A
choice does not have to be cached to be saved: the load prepares what is
missing. `vonkctl recipe download` and `vonkctl model download` prepare the
cache ahead of time.

## Load the profile

1. Choose a profile and its selected Sparks.
2. The Controller checks the exact assets and placement. The review lists the
   model download and the image build for each choice (each skipped when
   already cached), and progress reports them as the load's preparation. A
   preparation that cannot finish, for example a gated model without a token,
   is named as the reason the load waits.
3. Once the cache is ready, the Controller distributes the exact model and
   image assets to all selected Sparks in parallel. It verifies destination
   bytes and image identity and skips any already verified local copy.
4. The Controller stops and replaces conflicting workloads, then reports
   starting and serving readiness per Spark. Progress is durable and identifies
   the affected target on partial failure.

Load does not select a different revision or repair the NAS cache from a
Spark copy. Controller garbage
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

Load stops and replaces only the conflicting workloads in the requested
profile scope. It preserves completed transfers and reports what is actually
running on each Spark if the operation is partial. Retry resumes the durable
operation and reuses verified cache and Spark-local copies.

To go back, load an earlier profile. A profile always resolves to the newest
revision of each recipe it names, and the load prepares any asset that is no
longer cached through the same cache, distribution, replacement, and readiness
path; it never recovers from a Spark-local copy.
