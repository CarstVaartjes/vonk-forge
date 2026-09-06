# Make available, progress, updates and recovery

This extends the [compact list design](list-interface-design-2026-09-05.md) for
the first internal release. It is an implementation brief, not a claim that
all behavior below has shipped. Sol coordinates the backend, image preparation,
and web/CLI owners; root reviews their combined result.

## The operator's task

Select a Model or Recipe and make its required files available on the NAS.
Understand what is happening without opening logs. Refresh those files when a
new published definition is available, or explicitly download/build them again.
Run remains a separate action and automatically performs missing preparation.
The same operations and results must be available to a CLI agent.

The global recipe repository is the sole authored Model/Recipe authority.
Controller metadata refresh is automatic. There is no user-facing catalog sync,
import, qualification, or approval workflow in this feature.

## Actions and identity

| Action | Result |
| --- | --- |
| Make available, on a Model | Cache and verify every required file of the selected exact Model definition. Reuse verified shared files. |
| Make available, on a Recipe | Cache its complete required Model files and download or build its runtime image. Retain the verified image archive on the Controller/NAS. Do not start a workload. |
| Refresh | Check the latest global definition for the selected canonical identity and prepare its changed inputs. Report the selected old/new version and the amount reused. If unchanged, report Up to date. |
| Download again, on a Model | Fetch fresh bytes for the selected exact Model definition, even if already cached. Verify before replacing any valid cached object. |
| Download image again | Fetch a fresh copy of the selected published image using its declared immutable identity. |
| Rebuild image | Create a fresh build attempt from the selected Recipe's exact build inputs. Retain its new artifact receipt independently of existing running workloads. |

Refresh is not a hidden upstream rewrite. A Hugging Face revision or image pin
cannot silently change inside an existing Model/Recipe definition. An upstream
update becomes usable through a new definition in the global repository. If
the requested version has been removed or cannot be resolved, explain that
result instead of silently selecting a different creator, variant, or engine.

Put Make available on the row/details as the ordinary action. Available rows
show Available on NAS and offer Refresh. Put Download again and Rebuild image
in a small additional-actions menu. Offer only actions supported by the image
source: a published image can be downloaded; a build recipe can be rebuilt.
Avoid presenting the internal operation name Repair as the normal user label.

Preparation may use an appropriate enrolled ARM64 builder when the NAS cannot
build that runtime itself. Explain the actual builder in progress. The builder
returns the archive to the Controller; Sparks subsequently consume that local
archive. Do not imply a public registry push or that CPU-only NAS compilation
proves GPU execution. Availability ends at verified NAS files, not Spark staging.

## Compact progress

Preserve the paired lists and incumbent graphite/green design. Each selected
item has one inline status line under its normal comparison columns. Expand
only active rows enough for a thin progress track and one useful line of text.
Do not add tiles, a persistent sidebar, or a separate preparation dashboard.

Illustrative text, never production fixture claims:

- Downloading model · 42.6 / 168 GB · 87 MB/s · about 24 min left
- Downloading image · 6.1 / 19 GB · 64 MB/s
- Building image on Spark 3542 · Step 8 of 14 · Compiling attention kernels
- Receiving built image from Spark 3542 · 12.4 / 19 GB
- Verifying model files · 27 of 32 files
- Available on NAS · Model 168 GB · Image 19 GB

Use separate Model and Image subrows inside expanded Recipe progress because
they may advance concurrently. Shared Model transfers link to the same durable
operation instead of showing duplicate network work. A small activity count
may expose all selected operations in the existing activity view.

Display bytes and byte-based progress for transfers. File counts are additional
context; they must not make one tiny config file equal a 20 GB weight shard.
Show rate/ETA only when measured and credible. Cached/reused bytes are separate
from bytes transferred by this operation. Unknown totals use an indeterminate
track with actual bytes received. Build steps/log activity have no fabricated
percentage or ETA. Verification is an explicit stage, not premature success.

Persist the operation association and progress on the Controller. Navigation,
browser refresh, another client, or an API restart must recover the same state.
Polling/network failure in the browser means Progress unavailable, with a last
updated time; it must not turn a running download into Failed or Available.

At narrow widths, stack the same row content and retain status/action context.
Keep visible focus, real buttons, 44 px touch targets, and no page-wide
horizontal scrolling. Announce stage changes politely; do not read every byte
update through a screen reader. State words accompany all status colors.

## Errors and recovery

Show the affected item, a plain explanation, retained progress, and the next
action. Expandable technical details contain a stable error code, operation ID,
time, and a bounded sanitized log excerpt. Never expose tokens, Authorization
headers, signed download URL queries, or secret-file contents.

| Situation | Primary message and recovery |
| --- | --- |
| No Hugging Face credentials | Hugging Face access required. Link the exact model's access page and token setup instructions; offer Check access and resume. |
| Token rejected or scope insufficient | Hugging Face could not authorize this download. Explain token/account access checks without claiming the model is broken. |
| Account lacks model access | Request access on the model page with the account that owns the token, then Check access and resume. Do not promise a token alone grants access. |
| Provider throttling | Hugging Face is limiting requests. Show the actual retry time/countdown and resume automatically. |
| Interrupted network or provider outage | Download interrupted; completed files and partial progress are retained. Show automatic retry timing, then manual Retry after bounded attempts. |
| NAS space exhausted | Not enough NAS space. Show known required/free space and shortfall; otherwise state that the total requirement is unknown. Preserve existing valid cached files. |
| Size/digest mismatch | Download failed verification. Keep the previous valid object and offer Download again for the exact selected definition. Never mark mismatching bytes available. |
| Image build failure | Image build failed at the observed step. Show the useful sanitized error/log excerpt and Retry build. Preserve previously available image receipts. |
| Source/revision no longer available | The selected source could not be found. Offer Refresh for a newer global definition without silently changing the requested identity. |

Do not infer precise permission causes from an ambiguous HTTP 401/403. Explain
the likely checks and retain the provider's safe error category. Explicit
Check access and resume must re-read credentials and retry even when an auth
failure is correctly excluded from automatic transport retries.

Token setup uses the existing protected Controller secret-file mechanism and
documents service recreation where required. Public downloads work without a
token. A configured token can obtain authenticated quotas at canonical Hugging
Face endpoints; it must never be forwarded to CDN or unrelated hosts. The
browser must not present a token-entry form without an implemented secure
secret write path. Sparks receive verified local artifacts without HF tokens.

## Execution requirements

- Start with four bounded parallel file transfers across the Controller's
  model work queue. Deduplicate in-flight shared artifact digests and isolate
  per-transfer database sessions, temporary paths, and atomic publication.
- Long transfers must not block recipe reconciliation, other jobs, or the
  worker heartbeat. Persist claims and checkpoints for restart recovery.
- Image downloads and independent image builds also run concurrently with
  Model transfers and each other. Bound pulls by network/disk capacity and
  builds by each builder's available resources. Deduplicate identical image
  requests/build inputs; a saturated builder queues its work without blocking
  other builders or the Controller. Persist separate progress for every job.
- Respect provider Retry-After and rate-limit reset information. Persist the
  next eligible attempt instead of sleeping inside the main worker loop or
  immediately exhausting retries. Unaffected items/providers continue.
- Allow multiple selected operations. Disable only conflicting actions for the
  same item; do not disable the entire Model/Recipe list during one download.
- Reuse existing cache repair/download and image build/import primitives.
  Extend their typed API contracts rather than creating a second cache engine.
- Expose the same operation IDs, states, progress, errors, retry timing and
  recovery actions through the API, generated clients and CLI JSON output.
- Keep valid immutable cache objects and active workload receipts until their
  replacement is verified. Refreshing availability does not switch workloads
  or trigger automatic destructive cache cleanup.

## Acceptance evidence

Verify observable behavior using bounded HTTP/build fixtures: concurrent
transfers with a global cap, shared-digest deduplication, a responsive worker
during a slow transfer, interrupted transfer resume, correct byte accounting,
429 scheduling, credential recheck, space exhaustion, digest rejection, and a
forced download that preserves the old object until validation succeeds.

Verify image download, build, retry and forced rebuild through their real
Controller operations. Prove that an existing workload receipt remains bound
to its original image and that the new available receipt can be selected later.
Exercise simultaneous Model downloads, image pulls and independent builds;
verify resource limits, duplicate request reuse and worker responsiveness.
Do not substitute a simulated successful build for absent build infrastructure.

In the browser verify parallel selected rows, both Model and Image stages,
refresh/deep-link recovery, unknown totals, access-required recovery, build
errors, keyboard use and desktop/mobile rendering. Check the same operation
and error contract through CLI JSON. Fixture evidence, CI, Controller deployment,
and physical Spark acceptance remain separately reported.
