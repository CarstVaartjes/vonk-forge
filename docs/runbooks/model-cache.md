# NAS model cache preparation and repair

The Controller downloads immutable Model files once into `/state/model-cache`.
Enrolled Sparks receive complete, authorized manifests and bytes over the LAN;
they do not download Model payloads from upstream. Image archives are independent
and live under `/state/agent-artifacts/image-cache`. A Model can be downloaded
without selecting a Recipe, preparing an image, or requiring an online Spark.
The NAS is not a serving-time dependency after local installation completes.

## Prepare and inspect

Select the exact Model in Library and use Download, or use its canonical digest:

```bash
vonkctl cache download --model-content-sha256 MODEL_CONTENT_SHA256 --dry-run --json
vonkctl cache download --model-content-sha256 MODEL_CONTENT_SHA256 --apply --json
vonkctl cache list --json
vonkctl cache operations list --json
```

Preview distinguishes cached bytes from remaining upstream bytes and checks
actual filesystem free space against the configured reserve. Insufficient space
blocks preparation; it does not redirect Sparks to upstream. Concurrent workers
share one digest transfer. Partial files remain outside the published object
namespace and resume from their actual retained file lengths. Once all declared files
match their pinned sizes and SHA-256 identities, the cache entry is usable.

Transfers use ordinary buffered file writes. A separate sampler updates progress
once per second even when the next network read is blocked; no database session
or disk fsync runs per fragment. Progress describes observed received bytes, not
a promise of power-loss durability. Completion and interruption sync the file;
a restart reads the actual partial-file length and uses HTTP Range to resume.

Files download in parallel through the existing Controller-wide pool. HTTP files
of at least 64 MiB can use four concurrent byte ranges. Range segments resume
individually and are assembled before final verification. If the server ignores
Range, the downloader preserves the contiguous prefix and falls back to a normal
stream. Malformed ranges cannot be published, and truncated ranges remain
retryable. HF rate-limit cooldown is shared across new segment requests.

Range assembly requires temporary extra disk space. The worker reserves the
worst-case footprint across active range files and falls back to sequential
transfer when that footprint would consume the configured free-space reserve.

Cancel stops queued or active Model transfers and preserves partial files. The
API persists cancellation; the worker observes it independently, stops its active
streams, and cannot overwrite cancellation with a late progress or success update.
A new download request can reuse the retained files.

Successful verification is reused while a file's filesystem identity remains
unchanged, including during inventory reconciliation and LAN serving. A changed
file triggers verification again. Routine inventory and installation do not
rescan all unchanged model payloads. Per-node assignment, authorization,
manifest identity, transfer length and completion checks remain required.

## Credentials

Follow the [Hugging Face authentication guide](../model-cache-huggingface-auth.md)
for optional gated access. Only the NAS receives the upstream token. The
Controller sends it to the canonical Hugging Face authority and strips it from
CDN redirects. Sparks use their enrollment identity for NAS distribution.
Missing or denied account access is shown on the operation; after fixing access,
use its Check access and resume action to continue the same immutable transfer.

## Repair the same pin

```bash
vonkctl cache repair ARTIFACT_SET_SHA256 preview --json
vonkctl cache repair ARTIFACT_SET_SHA256 apply --plan-digest PLAN_DIGEST --apply --json
```

Repair checks reserve space for a new copy. Its partial files belong to that
repair and survive retries and Controller restarts. A new repair starts a fresh
transfer; it does not inherit a failed repair's bytes. Completed repair members
are retained across retries. Each replacement must satisfy the original pin
before an atomic overwrite; the existing pathname and already-open readers stay
available throughout publication. Failed transfers, mismatches and publication
failures preserve the last verified copy. Repair never changes the Model or
Recipe identity.

## Discover and accept upstream changes

The updates endpoint normally returns accepted catalog candidates without
network access. Explicit checks use
`GET /api/v1/model-cache/updates?check_upstream=true`, optionally filtered by
`artifact_set_sha256`. They fetch only repository metadata, once per repository
and pin within the result page. Up to four checks run concurrently after the
catalog DB session closes, within an eight-second page budget. Unfinished checks
report `model_cache.upstream_check_budget_exhausted`; accepted catalog candidates
remain available. Provider rate limiting is reported as a failed check with its
stable error code. `upstream_revisions` reports the pinned and
current upstream revisions, check time, and `current`, `update-available` or
`check-failed`. Provider failure does not hide accepted catalog candidates.

An upstream difference is a discovery result, not an installable update. Import
and review a new canonical Model manifest with the exact new revision and file
identities, then a new Recipe revision referencing that Model. The resulting
artifact set has a new immutable identity; the previous cached revision and its
installed consumers remain unchanged.

## Retention and evidence

Recipe uninstall retains successful Model and image cache entries. Eviction is
a separate preview/apply operation and rechecks installed Recipe, active-run and
in-flight references before deletion. Free filesystem bytes drive admission;
logical cache totals describe storage rather than determine free capacity.

Cache and per-node distribution operations expose source, bytes, cache hits and
transfer progress. Successful caching and LAN distribution prove preparation;
physical model runtime and multi-Spark fabric acceptance require the designated
hardware lane.
