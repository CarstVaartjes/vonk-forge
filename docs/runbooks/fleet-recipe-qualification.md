# Fleet recipe qualification

`vonk-fleet-qualify` inventories the reviewed public catalog and qualifies every
supported recipe against the actual enrolled fleet. It talks only to the
controller. Import, placement, build, distribution, install, load, smoke, stop,
and uninstall never require SSH access to a Spark.

The workflow is deliberately sequential. Only one recipe owns qualification
capacity at a time. The default cleanup stops the runtime to release memory,
while retaining downloaded model artifacts and caches for later variants.
Qualification proves that retention: after the first smoke the runner stops the
runtime, redeploys the same installation without an install operation, repeats
the exact digest-bound smoke, and stops it again.

## Preview the exact fleet plan

Install the repository tool and configure the same private controller token used
by `vonkctl`:

```bash
uv tool install .
chmod 600 .dev/admin-token
export VONK_CONTROL_URL=https://forge.example.test
export VONK_CONTROL_TOKEN_FILE="$PWD/.dev/admin-token"
export VONK_OPERATOR_JURISDICTION=NL

vonk-fleet-qualify \
  --ledger qualification-evidence.jsonl \
  > qualification-plan.json
```

Preview is the default. It reads the live fleet and exact public catalog commit,
previews every required import, and writes a hash-chained `plan.generated` record
to the append-only ledger. It does not import, place, install, or run anything.

The plan classifies recipes requiring more than two Sparks without trying them.
It also classifies insufficient online nodes, non-executable runtime contracts,
operator policy blocks, and machine-readable license territory restrictions.
A territorially restricted recipe fails closed when the operator jurisdiction is
unknown. `EU` restrictions match every EU member jurisdiction, including `NL`.

Use `--recipe PUBLISHER/SLUG` repeatedly for a bounded canary. The full catalog
is selected when the option is omitted.

### Parallel single-Spark lanes

One runner remains sequential within a lane, but the campaign coordinator runs
two explicitly disjoint single-Spark lanes concurrently. Use the coordinator
instead of maintaining two command lines by hand. Its manifest is the global
partition, but it cannot declare its own completeness. It names a checked-in,
reviewed qualification authority. Every recipe in that authority must occur in
exactly one lane, and no other recipe is accepted. The two lane names, exact
`spk_...` node IDs, ledgers, and plan outputs must all be distinct. Unknown
fields and duplicate JSON keys fail closed.

The current `nl-single-spark-9a6c7516` authority is bound to jurisdiction `NL`,
merged recipe-library commit `9a6c75167dbe6b66fd211cc5e37aaecdae175d00`, and
catalog-index SHA-256
`0697cc15026fd9789d00ba6289b9895e13ff1d68001c175c37981ecaa93f91b8`.
The canonical authority SHA-256 is
`e6609a4dcac0525ff2fc7dab84b3484c0a7f07233d0c05b6a74aedada7c77408`.
Its reviewed 82-recipe closure classifies every recipe exactly once: 57
actionable single-Spark recipes, five single-Spark recipes retained as original
above-envelope controls, nine single-Spark recipes blocked in NL, seven
dual-Spark recipes, and four recipes wider than the present fleet. The v2
authority names every non-actionable key, so an omission or overlap fails
closed instead of disappearing behind aggregate counts. The NL legal set
includes Hunyuan3D-Omni, HunyuanOCR, the five Hunyuan video/Foley recipes, and
both MiniMax H3 variants. The historical `nl-single-spark-f8d43aac`,
`nl-single-spark-d24bc1c8`,
`nl-single-spark-745a42b5`, `nl-single-spark-02ae8bb5`, and
`nl-single-spark-e996f025` authorities remain immutable for existing evidence;
do not use them against the refreshed catalog.

A later catalog, recipe, or license update requires a new reviewed authority
file and ID; do not edit an authority already named by evidence. The coordinator
rejects catalog repository or commit drift before publishing plans or applying
work.

The executable reviewed partition is
`config/qualification/nl-single-spark-9a6c7516.json`: 29 recipes on Spark3542
and 28 on Spark2297. It excludes every v2 legal- and capacity-blocked key while
keeping shared artifact families together. Lower-cost controls precede their
larger variants, MOVA 360p precedes 720p, and Step1X geometry precedes labeled
geometry and texture. Both lanes retain stopped installations and caches, but
the apply-time capacity plan remains authoritative for current disk fit.
The canonical manifest SHA-256 is
`ef4b919216d98849ebb453e8fbe74711010d675d8111e4d214083d32a3a8c684`.

Obtain the exact node identities with `vonkctl fleet list --json`, then create a
manifest. Relative ledger and plan paths are resolved from the manifest's
directory. The abbreviated JSONC below shows the shape; replace the comments
with an explicitly reviewed partition of every key in the named authority:

```jsonc
{
  "schema_version": 1,
  "qualification_authority": "nl-single-spark-9a6c7516",
  "options": {
    "jurisdiction": "NL",
    "cleanup": "stop",
    "operation_timeout_seconds": 86400,
    "poll_interval_seconds": 5
  },
  "lanes": [
    {
      "name": "spark-2297",
      "node_id": "spk_11111111111111111111111111111111",
      "recipes": [
        // Reviewed lane-one subset of the authority.
      ],
      "ledger": "evidence/spark-2297.jsonl",
      "plan_output": "plans/spark-2297.json"
    },
    {
      "name": "spark-3542",
      "node_id": "spk_22222222222222222222222222222222",
      "recipes": [
        // Every remaining authority key, exactly once.
      ],
      "ledger": "evidence/spark-3542.jsonl",
      "plan_output": "plans/spark-3542.json"
    }
  ]
}
```

Preview both lane plans in one operation:

```bash
vonk-fleet-qualify-campaign \
  --manifest config/qualification/nl-single-spark-9a6c7516.json \
  > campaign-preview.json
```

The coordinator writes both owner-only plan files and both hash-chained
`plan.generated` records only after it has validated the complete partition and
successfully generated both plans. Review the lane plan files, then copy the
single `campaign_digest` from the preview:

```bash
vonk-fleet-qualify-campaign \
  --manifest config/qualification/nl-single-spark-9a6c7516.json \
  --campaign-digest 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --apply
```

Apply freshly regenerates both plans and checks the global digest before either
lane can mutate the controller. The global digest binds the exact manifest and
both underlying plan digests; each lane still passes its own exact plan digest
to the existing qualification runner. After this all-or-nothing preflight, the
two lane runners execute concurrently and independently record resumable
controller operations in their own ledgers.

Both the coordinator and a directly invoked node-pinned
`vonk-fleet-qualify --node-id ...` hold the same per-node advisory lock for the
whole process lifetime. The lock is independent of ledger and working
directory, so a second local campaign for either Spark fails before controller
planning or mutation. Locks default to the current operator's private
`~/.local/state/vonk-forge/qualification-locks`; set
`VONK_QUALIFICATION_LOCK_DIR` only when all invocations use the same private
local directory. `VONK_QUALIFICATION_LOCK_DIR` and `XDG_STATE_HOME` must be
absolute so changing working directory cannot silently select a different lock.
Advisory locks coordinate processes on one operator host and user account; they
do not replace controller-side capacity authority for campaigns launched from
different hosts.

Keep recipes sharing large artifact identities in the same lane to retain dedup
benefits. Wait for both lanes to stop their runtimes before applying a separate
unpinned dual-Spark campaign; retained installations do not need to be removed.

## Apply and resume

Review `qualification-plan.json`, then pass its exact `plan_digest`:

```bash
PLAN_DIGEST=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef

vonk-fleet-qualify \
  --ledger qualification-evidence.jsonl \
  --plan-digest "$PLAN_DIGEST" \
  --apply
```

The digest binds immutable campaign intent: selected recipe keys and exact
catalog content digests, catalog authority, controller authority and fleet node
identities, jurisdiction and cleanup policy, and the qualification-fixture
manifest. It deliberately excludes observation timestamps, event cursors,
free-space readings, runtime state, and import/install mutations. Apply
reprojects the immutable rows and refuses catalog, fixture, authority, policy,
node-membership, or selected-revision drift. Expected telemetry and lifecycle
changes are recorded as snapshot evidence without invalidating resume. Every
controller mutation uses a deterministic idempotency key derived from the
intent digest, recipe identity, and step.

Before the first install, the runner imports/reads every actionable revision and
computes one fleet-wide retained-placement plan. It uses exact artifact identity
`(kind, repository, revision, include_paths)`, node roles, node-local dedup,
per-role image/cache/rollback persistence, staging and temporary-build peaks,
the controller's 10 GB or larger recipe safety floor, controller reservations,
pre-existing installs, and atomic per-node replication for dual-Spark recipes.
Bounded deterministic backtracking explores both recipe order and node
placement, prioritizes larger transient peaks, and minimizes projected
post-install imbalance so it does not repeatedly fill the first node. The
assignment binds its execution order, rank-zero builder, node-to-role/rank map,
installation identities, and every planned artifact-provider dependency. Resume reuses
it only while the selected candidate groups, provider graph, and allocatable
disk baseline remain valid; otherwise it records invalidation and safely
recomputes placement under the unchanged campaign intent. Fresh controller
telemetry and the cumulative planned prefix are checked against each assigned
node immediately before mutation. If a provider did not install, all consumers
that relied on its dedup are resource-blocked before mutation.
Missing telemetry, search exhaustion, or insufficient space blocks before
install. The runner never silently evicts another retained installation:
capacity evidence reports `automatic_eviction: false` and a concrete per-node
shortfall/alternative-plan disposition for operator review.

For scale, a documented fleet snapshot contained 3,275.9 GiB and 3,250.4 GiB
free on two 3,755.0 GiB nodes. The current one/two-Spark catalog is roughly
4.499 TB raw per-recipe installed bytes and 3.926 TB unique artifacts before
images and build temporary space. Those numbers are only an example: apply
always binds refreshed controller telemetry and the exact imported revisions.

The ledger records an operation ID before monitoring its durable controller
state. Rerun the same command after a process interruption: successful recipes
are skipped and a submitted operation is polled instead of submitted again.
Transient online, memory, capacity, and operation blockers are not mistaken for
completion; they are reconsidered against fresh observations. Immutable legal,
topology, policy, or special-fixture dispositions remain explicit.
One recipe failure does not prevent independent recipes from being attempted,
but the terminal record is `run.completed-with-failures` and the command exits
nonzero after the final residency inventory.
The ledger is owner-only (`0600`), append-only, sequence-numbered, hash-chained,
and flushed after every record. Tampering or a partial final record fails closed.

By default, every attempted smoke is stopped before the next recipe and the
installation is retained. Stop/release is attempted in `finally` after both the
initial smoke and warm redeploy, for transport errors as well as qualification
errors. Cleanup evidence never masks the original failure. The ledger records
the retained installation ID and ownership. A forced uninstall is explicit:

```bash
# Remove runner-owned installs as well as stopping runtime capacity.
vonk-fleet-qualify --cleanup uninstall --plan-digest "$PLAN_DIGEST" --apply

# Keep the recipe loaded only for an attended diagnostic session.
vonk-fleet-qualify --cleanup none --plan-digest "$PLAN_DIGEST" --apply
```

`--cleanup none` is not appropriate for an unattended full-catalog run.

## Additional operator blocks

Catalog license restrictions are authoritative and cannot be overridden. An
optional policy can add manual, legal, license, security blocks:

```json
{
  "schema_version": 1,
  "blocked_recipes": {
    "vonk/example": {
      "classification": "manual",
      "detail": "Awaiting model-owner approval"
    }
  }
}
```

```bash
vonk-fleet-qualify --policy qualification-policy.json
```

## Evidence captured

Successful records retain the catalog repository and commit, recipe content
SHA-256, immutable recipe revision, image/operation results, mapping and plan
digests, exact Spark node identities, build, installation, run identities,
published endpoint evidence, bounded smoke-response digest, and cleanup results.
Rejected placement projections are retained with their controller reason codes,
including disk, memory, fabric, freshness, and active-reservation evidence.
In the campaign's outer `finally`, `run.residency-inventoried` paginates the
whole Library and records every installation consuming disk, including blocked,
failed, special-fixture, and stale-revision dispositions. Each installation is
reported separately with recipe/revision, nodes, state, selected-revision match,
deployability, and evidence gaps. The same record includes per-node used/free,
retained artifact dedup, reservations, assignment fit, and whether all feasible
installs remain resident. A partial inventory is explicitly marked incomplete
without masking an earlier campaign error.

The runner has a typed artifact-job smoke boundary and uses the dedicated
`/api/v1/recipes/job-runs` activation route. A job recipe still requires an
exact qualification-matrix fixture: typed input slots with path, MIME type,
SHA-256 and bytes; parameters; output limits; timeout; and output assertions.
Until that recipe-specific contract is supplied, the recipe is recorded as
`artifact_job.fixture_contract_required`, cleaned up, and not reported as
successful. This is intentional: activating an artifact worker without proving
an output is not qualification.

The checked-in schema-v2 qualification manifest currently binds all 42 artifact recipes
to executable fixtures with immutable provenance and no unresolved
special-artifact dispositions. Artifact fixtures can declare multiple named
cases; every case receives its own durable controller job, idempotency key,
ledger event namespace, semantic output validation, and warm-redeploy rerun.
The current 56 cases cover one- and two-reference Qwen edits, MOVA with and
without a reference, one- and two-frame MOSS sessions, MiniMax FL2VA text and
keyframe modes, both optional LTX 2.5 FP8 profiles, Wan-Dancer controls, and an
explicit HunyuanOCR config bound. SkinTokens uses a deterministic mesh-only
derivative of Khronos' CC-BY-4.0 RiggedFigure with its transformation pinned;
Step1X texture uses a deterministic CC0 triangle cube and reference image.
Wan-Dancer uses a deterministic, digest-pinned one-second PCM music fixture.
Hunyuan Foley uses a deterministic one-second, 25-frame, 25 fps H.264 fixture,
so SyncFormer is exercised rather than special-blocked. HunyuanOCR uses the
digest-pinned digit-7 PNG and validates the exact safe ZIP members, closed
manifest authority, sampling receipt, UTF-8 Markdown, character count, and OCR
semantic result.
MOSS realtime uses closed one- and two-frame schema-v1 sessions whose events
reference authenticated `frames`-slot PNGs; acceptance checks each exact replay
MP4 and its ordered model-revision/frame-ack/session-stop JSONL transcript.

Artifact acceptance is physical, not extension-based. PNG validation checks
CRC, exact dimensions, bit depth/color type, and bounded decoded scanlines; WAV
checks PCM channels/rate/width/frame-derived duration; MP4 requires `ffprobe`
counted frames and stream metadata plus a bounded `ffmpeg` decode; GLB uses the
shared strict embedded GLB2 geometry/material/skin validator; ZIP rejects unsafe
paths, links, encryption, duplicates, oversized entries, and expansion bombs;
JSON/JSONL reject duplicate keys and non-finite numbers; semantic receipts use
closed schemas and cross-file digest checks. If a required parser is unavailable
or an allowed media type lacks a semantic assertion, qualification fails closed.

Service acceptance is also digest-bound. The 27 one- or two-Spark service
recipes select reviewed cases from a runner-owned template registry. Cases
verify the exact `/models` alias and then capability-specific arithmetic,
structured tool calls, reasoning separation, vision, OCR, or inert GUI-action
contracts. Request paths, response bounds, fixture data URIs, assertions, and
recipe digests are operator-invariant. Stress and recovery tiers remain
explicitly separate from the safe base smoke; a generic nonempty chat can never
qualify a service recipe.
