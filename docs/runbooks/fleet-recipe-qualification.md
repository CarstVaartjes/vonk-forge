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

One runner remains sequential, but two explicitly disjoint single-Spark
campaigns may run concurrently on separate Sparks. Pin each campaign with
`--node-id`. A pinned campaign requires one or more explicit `--recipe`
selections, and every selected recipe must declare exactly one Spark. The node
allowlist is part of the immutable campaign digest; unknown node IDs, dual-Spark
recipes, an eligible-placement replan outside the allowlist, or an apply-time
selection outside it fail before mapping or build.

Use distinct recipe sets, exact controller node IDs, plan files, and ledgers.
Obtain the `spk_...` identities with `vonkctl fleet list --json`, then export
the two values used below:

```bash
vonk-fleet-qualify \
  --ledger evidence/spark-2297.jsonl \
  --node-id "$SPARK_2297_NODE_ID" \
  --recipe vonk-forge/example-a-single \
  --recipe vonk-forge/example-b-single \
  > plan-spark-2297.json

vonk-fleet-qualify \
  --ledger evidence/spark-3542.jsonl \
  --node-id "$SPARK_3542_NODE_ID" \
  --recipe vonk-forge/example-c-single \
  --recipe vonk-forge/example-d-single \
  > plan-spark-3542.json
```

Review both previews and apply each with the same recipe/node arguments, its
own `--plan-digest`, and `--apply`. They can then run as separate processes.
Never share a ledger or overlap node/recipe sets between lanes. Keep recipes
sharing large artifact identities in the same lane to retain dedup benefits.
Wait for both lanes to stop their runtimes before applying a separate unpinned
dual-Spark campaign; retained installations do not need to be removed.

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

The checked-in qualification manifest currently binds all 38 artifact recipes
to executable fixtures with immutable provenance and no unresolved
special-artifact dispositions. SkinTokens uses a deterministic mesh-only
derivative of Khronos' CC-BY-4.0 RiggedFigure with its transformation pinned;
Step1X texture uses a deterministic CC0 triangle cube and reference image.
Wan-Dancer uses a deterministic, digest-pinned one-second PCM music fixture.
Hunyuan Foley uses a deterministic one-second, 25-frame, 25 fps H.264 fixture,
so SyncFormer is exercised rather than special-blocked. HunyuanOCR uses the
digest-pinned digit-7 PNG and validates the exact safe ZIP members, closed
manifest authority, sampling receipt, UTF-8 Markdown, character count, and OCR
semantic result.
MOSS realtime uses a closed one-frame schema-v1 session whose event references
the authenticated `frames`-slot PNG; acceptance checks the exact replay MP4 and
the ordered model-revision/frame-ack/session-stop JSONL transcript.

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
