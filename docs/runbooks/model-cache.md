# Controller model and recipe cache

The Controller/NAS cache is the trusted preparation authority. It stores and
verifies the exact immutable model files and required runtime image for a
profile choice. Sparks receive authorized copies over the LAN for execution;
those copies are derived, replaceable state and never an authority or fallback
source for profiles or cache repair.

The commands below describe current behavior. Operators never edit JSON or
database status to recover work.

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
allows, for up to 32 simultaneous data requests. There is no per-connection
speed limit.

## Transfer to Sparks

The Controller authorizes each Spark request (assignment, node, expiry, range)
and names the stored file; Caddy then serves the bytes and the range from a
read-only mount of the model and image stores. The Controller never reads them
on this path. Each Spark fetches files in 64 MiB ranges over HTTP/1.1, with an
adaptive number of ranges in flight (four to start, at most eight, more only
while throughput grows), and preallocates and flushes what it writes so a large
model neither fragments the disk nor fills the page cache.

About 280 MB/s is the ceiling on 2.5 GbE, shared by every Spark copying at the
same time. When a copy runs far below it, follow the
[transfer throughput runbook](transfer-throughput.md): it measures the network,
the Controller round trip, the NAS disks and CPU separately with
`scripts/measure-transfer` (on a Spark) and `scripts/measure-nas-serving` (on
the NAS).

On Btrfs the Controller marks the model and image directories no-copy-on-write
(which also disables compression) when it starts. Only files created later
inherit this; existing files keep their attributes and are not rewritten.
Other filesystems ignore it.

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

Scripts may inspect the review first, then submit with explicit consent. The
CLI fetches and binds the latest review automatically:

```bash
vonkctl model remove MODEL --yes
```

Recipe scripts also pass the chosen retention behavior. The digest is internal
protocol bookkeeping; authorization and exact target identity remain
Controller-enforced. See the
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
per-Spark progress and readiness. A runtime image is identified by its
content, so every recipe revision with the same executable build inputs (an
editorial revision, for instance) runs the same cached image archive; no
per-revision permission is recorded or required, and nothing triggers another
image download or build.

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

## Unused storage is removed only when disk is short

Installations, image receipts and cached models can always be fetched again, so
nothing is removed because it has been idle; the worker removes them only to make
room. It does so when work was refused for lack of disk (a profile load, an
install, a build or a model download asked for free space on a Spark or the NAS)
and, as a safety net, when a Spark or the NAS runs low on free space on its own.
A profile change alone removes nothing.

A Spark or the NAS counts as low when its free space is below the larger of 10 %
of its capacity and the largest install (Spark) or model set (NAS) it has held,
at most 25 % of its capacity (`STORAGE_LOW_FREE_*` in `settings.py`; these are
tuning constants, not `.env` values). A refused request asks for more than that.
Either way the worker frees the shortfall plus a reserve (2 % of the capacity, at
least 5 GiB) and stops:

- **Spark installations** (state `installed`). This is a real uninstall on the
  Sparks, queued like `vonkctl recipe uninstall`; its model files go with it
  unless another installation on that Spark needs them, and the agent then also
  deletes the Spark's shared store copy of each model file no installation links
  any more (installations hard-link store objects, so removing an installation
  alone frees none of its model bytes). It takes no new workload intent, so it
  cancels no other order and never disturbs recovery of a running workload.
  Installations of one model on a Spark go together or not at all.
- **Runtime image receipts** in the NAS `image-cache`; the image store then
  reclaims their blobs at once.
- **Cached model files**, through the same durable, fenced removal as `vonkctl
  model remove`.

Least recently used goes first; items used in the last 24 hours go only after
everything older. Nothing is removed while a saved profile (loaded or not) points
to it (the newest revision of a recipe a profile names), a workload runs from it,
or a live or recent operation names it. A recipe the catalog offers but no profile
names is only a download away, so its model and image can go. Every removal is
proven unused again while the Sparks or artifact gates are locked, so a load that
starts meanwhile is never raced. One item that cannot be removed is kept for the
next pass and does not stop the others.

A removal queued on a Spark is given time to land and show in a fresh inventory
before the next one, so the Spark's own reported free space (not a guess at what
hard links free) decides whether more must go. A round that frees less than half
of what it promised pauses eviction there for 15 minutes. If everything that may
go would still not cover a refused request, nothing is removed.

Disk admission counts what a Spark will actually write: while an installation of
a model exists on a Spark, its shared store holds that model's files and a new
installation links them, so a load's disk claim leaves them out. For the same
reason an installation of a model a saved profile's recipe needs is never
evicted (removing the last installation of a model deletes the store's copy).

A load that waits for disk is not failed: it shows `storage.evicting` (bytes
needed and bytes that can be freed) while the Controller frees room, or
`storage.insufficient_after_eviction` when all unused installations together
would not be enough (stop or remove something on that Spark). It resumes by
itself on its next retry once the Spark reports enough free space.

Each pass that removes something, or whose answer changes, logs one
`unused_storage.eviction_pass` line with, per Spark or NAS, why it ran, the bytes
needed and freeable, what it expects to free, what it removed, and why the rest
was kept.

A profile's retention (`keep-cached` or `exact`) only decides whether *loading*
that profile removes installations outside its scope; it never promised to keep
what no profile points to.

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
