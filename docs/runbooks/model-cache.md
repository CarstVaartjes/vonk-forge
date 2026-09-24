# Controller model and recipe cache

The Controller/NAS cache is the trusted preparation authority. It stores and
verifies the exact immutable model files and required runtime image for a
profile choice. Sparks receive authorized copies over the LAN for execution;
those copies are derived, replaceable state and never an authority or fallback
source for profiles or cache repair.

The commands below describe current behavior. The
[storage and coordination plan](../plans/resilient-artifact-storage.md) changes
internal checkpoint/availability ownership while preserving these exact-asset,
authorization, retry, and cancellation guarantees. It does not require operators
to edit JSON or database status to recover work.

## Browse, download, and refresh

Use the exact selector shown by the list:

```bash
vonkctl model
vonkctl model library
vonkctl model download MODEL
vonkctl recipe library
vonkctl recipe download RECIPE
vonkctl recipe update --all
```

A model can be prepared without a recipe or an online Spark. Recipe preparation
also prepares its image and downloads a missing model. Repeating an active
download follows it; repeating a failed transfer resumes it; repeating a
completed download refreshes it. No separate repair command or force flag is
required. A failed refresh must preserve the last verified copy. Profile
choices may reference only complete, verified Controller/NAS cache entries.

Only fully verified objects are published. Partial transfers remain outside
the published namespace. Admission uses actual filesystem free space, reserve,
and temporary transfer/build requirements—not just logical model size.
Unknown sizes are reported as unknown. Describing a complete set trusts its
durable publication receipts and checks managed file metadata. Each download
request verifies the bytes of its selected file without scanning unrelated
weights. An in-progress refresh keeps the previous verified copy available.

Upstream preparation, including Hugging Face downloads, shares a bounded pool
of eight concurrent files across active cache operations. Each file of at least
64 MiB may use four resumable HTTP range requests when temporary disk space
allows, for up to 32 simultaneous data requests. The
`VONK_MODEL_CACHE_PARALLEL_DOWNLOADS` setting controls the file pool, accepts
1 through 16, and defaults to 8. It does not set a per-connection speed limit.

## Remove and cancel

Inspect the Controller-owned impact before accepting removal:

```bash
vonkctl model remove MODEL --review
vonkctl recipe remove RECIPE --keep-model --review
vonkctl recipe remove RECIPE --with-model --review
```

The review shows exact assets and storage observations, shared objects retained,
references, active work and blockers. In a terminal, omit `--review` to display
that impact and answer the confirmation. Recipe removal always requires the
explicit `--keep-model` or `--with-model` choice.

Scripts must obtain the review first, then pass its exact digest with explicit
consent. For example, after reviewing the model response:

```bash
vonkctl model remove MODEL --review-digest REVIEW_SHA256 --yes
```

Replace `REVIEW_SHA256` with the digest returned by the review. Recipe scripts
also pass the same retention choice they reviewed. Changed effects are refused
and require a new review; `--yes` alone is insufficient. See the
[CLI runbook](vonkctl.md) for complete JSON examples and request-key recovery.

Removal persists exact intent before deleting bytes and reports accepted,
waiting, partial and completed work separately. A lost response reconnects to
the same request. Local timeout or interruption stops observation only. Active
preparation, saved profiles and workload references can block removal; eviction
does not cancel their owners. Use the noun's explicit `cancel` command for
cancellation and observe its settlement before reviewing removal again.

Shared physical objects remain while another cache entry requires them. The
review names the protecting owner; successful removal of the selected entry
does not imply those shared bytes were reclaimed. Worker recovery retains its
original scope and checkpoints, and late publication cannot cross a deletion
fence. No Spark-local copy becomes an authority or fallback for profile cache
readiness. A profile with a missing asset exposes its cache-preparation action
instead of silently fetching upstream.

NAS garbage collection remains separate from explicit removal and may collect
unused local objects only after authoritative references are gone. It does not
require Spark copies to be deleted to finish a Controller cache operation.

Profiles persist exact cached model/recipe-image choices and show cache state
alongside observed Spark running state. Loading a profile does not resolve a
newer cache entry or select from a Spark copy. Cache updates and garbage
collection do not change running workloads; a later apply must pass the cache
gate before distributing exact assets to selected Sparks in parallel. Apply
skips verified local copies, then stops/replaces workloads and reports durable
per-Spark progress and readiness. For a multi-Spark recipe, the Controller
records image authorization for every compiled role and rank before installing
the group. Those authorizations reuse the same cached image archive; they do
not trigger another image download or build.

Active Spark transfers renew their node- and plan-bound download authorization
through the existing authenticated job heartbeat. A large copy can continue
beyond the initial hour while the agent still owns its operation lease.
Revoked or expired assignments, stale attempts, and cancellation requests do
not renew access; a stopped heartbeat lets the last authorization expire.

Bulk downloads use bounded 1 MiB serving blocks and a 1 MiB Spark disk-write
buffer to avoid scheduling a filesystem operation for each small network
chunk. The Spark flushes the buffer and syncs the completed file before
verification and acceptance. Network retries retain the writer; after a
process restart, resume uses the actual partial file length on disk.

Each Spark fetches up to sixteen independent objects concurrently through the
same authenticated client, starting the largest files first to avoid leaving
a large image archive until the other transfers have finished. Every object
keeps its own range, digest check, and
resumable partial file. Progress combines per-object bytes and counts an item
complete only after verification; receipt ordering follows the manifest even
when transfers finish out of order. A failed or cancelled download stops the
remaining transfers and retains their on-disk checkpoints.

## Credentials and evidence

See [Hugging Face authentication](../model-cache-huggingface-auth.md) for gated
models. Upstream credentials stay on the Controller and are not sent to Sparks
or unrelated redirect authorities. After correcting access, repeat download.

Progress reports measured cache preparation, parallel distribution, workload
replacement, and per-Spark readiness. Successful caching is Controller/NAS
preparation evidence; distribution and runtime quality are separate target
states, and multi-Spark fabric acceptance still requires the designated
hardware lane.

See [the CLI runbook](vonkctl.md) for output, selectors, profiles, and automation.
