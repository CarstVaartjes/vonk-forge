# Controller model and recipe cache

The Controller/NAS cache is the trusted preparation authority. It stores and
verifies the exact immutable model files and required runtime image for a
profile choice. Sparks receive authorized copies over the LAN for execution;
those copies are derived, replaceable state and never an authority or fallback
source for profiles or cache repair.

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
Unknown sizes are reported as unknown.

## Remove and cancel

```bash
vonkctl model remove MODEL
vonkctl recipe remove RECIPE
vonkctl recipe remove RECIPE --with-model
```

Removal cancels the corresponding download or build and removes the Controller
cache entry when it is no longer referenced. It does not use a Spark-local copy
to restore the entry. Applying a profile with a missing or invalidated entry is
blocked with the missing asset and a cache-preparation action; it does not
silently fetch upstream. A late worker completion must not republish a removed
entry.

When removing the last cached recipe for a model, the CLI asks whether to remove
the model too. Use `--keep-model` or `--with-model` to make that choice explicit.
Shared physical objects remain while another cache entry, saved profile, active
workload, or preparation operation still requires them. NAS garbage collection
may remove unused local model objects after those references are gone. It does
not make Spark copies authoritative or require their deletion to complete a
cache operation.

Profiles persist exact cached model/recipe-image choices and show cache state
alongside observed Spark running state. Loading a profile does not resolve a
newer cache entry or select from a Spark copy. Cache updates and garbage
collection do not change running workloads; a later apply must pass the cache
gate before distributing exact assets to selected Sparks in parallel. Apply
skips verified local copies, then stops/replaces workloads and reports durable
per-Spark progress and readiness.

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
