# Physical qualification: NL catalog 02ae8bb5

This is the reviewed, post-deployment execution matrix for qualification
authority `nl-single-spark-02ae8bb5`. It must not be run until the Controller
and both Sparks have been upgraded and their fresh telemetry has been reviewed.
It uses Controller APIs only; no SSH access to a Spark is required.

## Bound campaign identity

| Item | Reviewed value |
| --- | --- |
| Recipe repository | `CarstVaartjes/vonk-forge-recipes` |
| Recipe commit | `02ae8bb5065919e263183f59637f4d8954a7334a` |
| Catalog index SHA-256 | `165be2692acafa1fe51345d83dbdd3b3d07ba308463a031a98e9bc563e0da5c5` |
| Qualification authority | `nl-single-spark-02ae8bb5` |
| Canonical campaign-manifest SHA-256 | `7cbf48df404bd1bd656579c0a8823189abdfb04703e09808f0549874ca7e1939` |
| Spark3542 | `spk_2818d189042b4c77aefa7796f4befd23` |
| Spark2297 | `spk_9a86fdbab116442ab6707bf4181a3c1c` |
| Runtime cleanup | `stop` (stop both smokes, retain installations and caches) |

The executable manifest is
`config/qualification/nl-single-spark-02ae8bb5.json`. The campaign coordinator
rejects any omission, substitution, duplicate, node-ID reuse, or ledger/plan
path collision before contacting the Controller. Its campaign digest then
binds the manifest, reviewed authority, and both freshly generated lane-plan
digests. The split keeps known shared immutable artifact families together;
the fresh `capacity.plan.created` records remain authoritative for actual fit.

## Single-Spark lanes

Run both lanes through the campaign coordinator. They execute concurrently but
are pinned to distinct controller node identities for their entire process
lifetime.

### Spark3542 lane (29)

1. `vonk-forge/deepseek-v4-flash-0731-ds4-dspark-latency-single`
2. `vonk-forge/deepseek-v4-flash-0731-ds4-single`
3. `vonk-forge/deepseek-v4-flash-0731-mia-sparkinfer-single`
4. `vonk-forge/deepseek-v4-flash-0731-sparkinfer-single`
5. `vonk-forge/gemma-4-26b-a4b-vllm-single`
6. `vonk-forge/hunyuan-video-15-distilled-diffusers-single`
7. `vonk-forge/hunyuan-video-15-i2v-step-distilled-diffusers-single`
8. `vonk-forge/hunyuan-video-15-t2v-diffusers-single`
9. `vonk-forge/hunyuan-video-foley-xl-pytorch-single`
10. `vonk-forge/hunyuan-video-foley-xxl-pytorch-single`
11. `vonk-forge/laguna-xs-2-1-nvfp4-vllm-single`
12. `vonk-forge/lfm2-5-vl-3b-vllm-single`
13. `vonk-forge/ling-3-0-flash-dspark-sglang-single`
14. `vonk-forge/ltx-2-19b-dev-bf16-diffusers-single`
15. `vonk-forge/ltx-2-19b-dev-fp4-pytorch-single`
16. `vonk-forge/ltx-2-19b-distilled-diffusers-single`
17. `vonk-forge/ltx-2-19b-distilled-fp8-diffusers-single`
18. `vonk-forge/ltx-2-3-22b-distilled-1-1-diffusers-single`
19. `vonk-forge/ltx-2-5-22b-distilled-bf16-diffusers-single`
20. `vonk-forge/moss-vl-realtime-11b-pytorch-single`
21. `vonk-forge/mova-360p-diffusers-single`
22. `vonk-forge/mova-720p-diffusers-single`
23. `vonk-forge/muse-glimmer-30b-bf16-vllm-single`
24. `vonk-forge/nemotron-3-5-lightning-30b-a3b-vllm-dspark-latency-single`
25. `vonk-forge/nemotron-3-5-lightning-30b-a3b-vllm-single`
26. `vonk-forge/nemotron-3-nano-30b-a3b-vllm-single`
27. `vonk-forge/nemotron-3-super-120b-a12b-vllm-single`
28. `vonk-forge/qwen3-5-9b-vllm-single`
29. `vonk-forge/skintokens-pytorch-single`

### Spark2297 lane (30)

1. `vonk-forge/flux-2-klein-4b-comfyui-single`
2. `vonk-forge/flux-2-klein-4b-nvfp4-comfyui-single`
3. `vonk-forge/laguna-s-2-1-nvfp4-vllm-single`
4. `vonk-forge/minimax-h3-diffusers-single`
5. `vonk-forge/nemotron-3-nano-omni-30b-a3b-vllm-single`
6. `vonk-forge/nvidia-qwen-image-flash-diffusers-single`
7. `vonk-forge/pixal3d-pytorch-single`
8. `vonk-forge/qwen-image-2512-comfyui-single`
9. `vonk-forge/qwen-image-2512-diffusers-single`
10. `vonk-forge/qwen-image-2512-lightning-diffusers-single`
11. `vonk-forge/qwen-image-edit-2511-comfyui-single`
12. `vonk-forge/qwen-image-edit-2511-diffusers-single`
13. `vonk-forge/qwen-image-edit-2511-fp8mixed-comfyui-single`
14. `vonk-forge/qwen-image-edit-2511-lightning-diffusers-single`
15. `vonk-forge/qwen-image-layered-diffusers-single`
16. `vonk-forge/qwen3-6-27b-vllm-single`
17. `vonk-forge/qwen3-6-35b-a3b-nvfp4-vllm-single`
18. `vonk-forge/qwen3-8-27b-fp8-vllm-single`
19. `vonk-forge/qwen3-8-27b-nvfp4-dspark-sglang-single`
20. `vonk-forge/qwen3-8-27b-vllm-single`
21. `vonk-forge/step1x-3d-geometry-pytorch-single`
22. `vonk-forge/step1x-3d-label-geometry-pytorch-single`
23. `vonk-forge/step1x-3d-texture-pytorch-single`
24. `vonk-forge/trellis-2-4b-pytorch-single`
25. `vonk-forge/triposg-pytorch-single`
26. `vonk-forge/ui-mate-27b-vllm-single`
27. `vonk-forge/wan-2-2-i2v-14b-comfyui-single`
28. `vonk-forge/wan-2-2-t2v-14b-comfyui-single`
29. `vonk-forge/wan-2-2-ti2v-5b-comfyui-single`
30. `vonk-forge/wan-dancer-14b-pytorch-single`

## Preflight, preview, and apply

Run from the deployed platform checkout with `VONK_CONTROL_URL` and the private
`VONK_CONTROL_TOKEN_FILE` already configured. Do not put a bearer token on the
command line. Keep this state directory; it contains resumable, hash-chained
evidence.

```bash
cd /opt/vonk-forge
umask 077
export QUALIFICATION_ROOT="$PWD/.state/qualification/nl-single-spark-02ae8bb5"
mkdir -p "$QUALIFICATION_ROOT/controller" "$QUALIFICATION_ROOT/evidence" \
  "$QUALIFICATION_ROOT/plans" "$QUALIFICATION_ROOT/status" \
  "$QUALIFICATION_ROOT/dual"
test -n "$VONK_CONTROL_URL"
test -n "$VONK_CONTROL_TOKEN_FILE"
test -r "$VONK_CONTROL_TOKEN_FILE"
```

First capture fresh Controller identity, node authority, telemetry, and current
Library residency. Stop if either exact node ID is absent, offline/stale, not
ready, or lacks fresh disk and memory telemetry. The preview rejects recipes
whose declared runtime memory cannot fit the pinned Spark. The cumulative
retained-disk fit is computed during apply, after any required idempotent catalog
imports have supplied the exact local placement inputs and before the first
installation is submitted.

```bash
uv run --frozen vonkctl fleet agents --json \
  > "$QUALIFICATION_ROOT/controller/agents-before.json"
uv run --frozen vonkctl fleet show spk_2818d189042b4c77aefa7796f4befd23 --json \
  > "$QUALIFICATION_ROOT/controller/spark-3542-before.json"
uv run --frozen vonkctl fleet telemetry spk_2818d189042b4c77aefa7796f4befd23 \
  --range 24h --json > "$QUALIFICATION_ROOT/controller/spark-3542-telemetry.json"
uv run --frozen vonkctl fleet show spk_9a86fdbab116442ab6707bf4181a3c1c --json \
  > "$QUALIFICATION_ROOT/controller/spark-2297-before.json"
uv run --frozen vonkctl fleet telemetry spk_9a86fdbab116442ab6707bf4181a3c1c \
  --range 24h --json > "$QUALIFICATION_ROOT/controller/spark-2297-telemetry.json"
uv run --frozen vonkctl library list --all --json \
  > "$QUALIFICATION_ROOT/controller/library-before.json"
```

Preview both lanes. This contacts the Controller only to read and plan; it does
not mutate it. It writes the two exact plan files and one `plan.generated`
record to each lane ledger.

```bash
uv run --frozen vonk-fleet-qualify-campaign \
  --manifest config/qualification/nl-single-spark-02ae8bb5.json \
  > "$QUALIFICATION_ROOT/campaign-preview.json"
jq -e '
  .mode == "preview" and
  .qualification_authority == "nl-single-spark-02ae8bb5" and
  .campaign_manifest_sha256 == "7cbf48df404bd1bd656579c0a8823189abdfb04703e09808f0549874ca7e1939" and
  ([.lanes[].recipe_count] | sort) == [29, 30] and
  ([.lanes[].node_id] | sort) == ([
    "spk_2818d189042b4c77aefa7796f4befd23",
    "spk_9a86fdbab116442ab6707bf4181a3c1c"
  ] | sort)
' "$QUALIFICATION_ROOT/campaign-preview.json"
export CAMPAIGN_DIGEST
CAMPAIGN_DIGEST="$(jq -er '.campaign_digest' "$QUALIFICATION_ROOT/campaign-preview.json")"
```

Review both plan files before apply. Require exact catalog commit/content
identities, current controller and fleet authority, the two node pins, no
preview blockers, complete smoke fixtures, and `cleanup=stop`. This concise
summary makes the declared retained-capacity inputs easy to compare without
mistaking their simple totals for an artifact-deduplicated placement proof:

```bash
for PLAN in \
  "$QUALIFICATION_ROOT/plans/spark-3542.json" \
  "$QUALIFICATION_ROOT/plans/spark-2297.json"
do
  jq -e '
    .mode == "preview" and
    .options.cleanup == "stop" and
    (.options.allowed_node_ids | length) == 1 and
    ([.recipes[].blockers[]?] | length) == 0 and
    all(.recipes[];
      (.maximum_installed_bytes_per_node | type) == "number" and
      (.temporary_build_bytes_per_node | type) == "number" and
      (.disk_requirements_by_role | type) == "object" and
      (.planned_actions | index("retain-installation")) != null)
  ' "$PLAN" >/dev/null
  jq '{
    lane,
    node_id: .options.allowed_node_ids[0],
    recipe_count: (.recipes | length),
    sum_of_per_recipe_maximum_installed_bytes:
      ([.recipes[].maximum_installed_bytes_per_node] | add),
    largest_temporary_build_bytes:
      ([.recipes[].temporary_build_bytes_per_node] | max)
  }' "$PLAN"
done
```

Preview does not create `capacity.plan.created`: a complete retained placement
depends on exact imported revisions, node-local artifact deduplication, existing
installations, and refreshed allocatable disk. Apply performs those idempotent
preparations, then writes one `capacity.plan.created` record per lane before its
first install. If either lane cannot prove a complete fit, it records
`capacity.blocked` with the shortfall and stops before installation;
`automatic_eviction` remains false. There is no operator-review pause between a
successful capacity calculation and execution, so keep the apply process
attached and monitor both ledgers from the second terminal below. If the
catalog, fixtures, fleet membership, or immutable plans drift, re-preview and
review a new campaign digest.

```bash
uv run --frozen vonk-fleet-qualify-campaign \
  --manifest config/qualification/nl-single-spark-02ae8bb5.json \
  --campaign-digest "$CAMPAIGN_DIGEST" --apply \
  > "$QUALIFICATION_ROOT/campaign-apply.json"
```

The apply command must remain attached. If the operator process is interrupted,
rerun the same apply command with the same reviewed digest; durable operation
IDs and the ledgers make it resume rather than resubmit completed work. Do not
start another qualification process against either Spark.

Use a second terminal for read-only status snapshots:

```bash
cd /opt/vonk-forge
export QUALIFICATION_ROOT="$PWD/.state/qualification/nl-single-spark-02ae8bb5"
uv run --frozen vonkctl activity jobs --status running --limit 100 --all --json \
  > "$QUALIFICATION_ROOT/status/jobs-running.json"
uv run --frozen vonkctl fleet show spk_2818d189042b4c77aefa7796f4befd23 --json \
  > "$QUALIFICATION_ROOT/status/spark-3542.json"
uv run --frozen vonkctl fleet show spk_9a86fdbab116442ab6707bf4181a3c1c --json \
  > "$QUALIFICATION_ROOT/status/spark-2297.json"
tail -n 20 "$QUALIFICATION_ROOT/evidence/spark-3542.jsonl"
tail -n 20 "$QUALIFICATION_ROOT/evidence/spark-2297.jsonl"
```

Before proceeding to dual-Spark recipes, require both ledgers to end in
`run.completed`, require 59 unique successful recipe keys, and verify no runtime
remains active. A `run.completed-with-failures` or incomplete residency
inventory is not acceptance.

```bash
SPARK_3542_PLAN_DIGEST="$(jq -er '.plan_digest' \
  "$QUALIFICATION_ROOT/plans/spark-3542.json")"
SPARK_2297_PLAN_DIGEST="$(jq -er '.plan_digest' \
  "$QUALIFICATION_ROOT/plans/spark-2297.json")"
jq -s -e \
  --arg spark3542 "$SPARK_3542_PLAN_DIGEST" \
  --arg spark2297 "$SPARK_2297_PLAN_DIGEST" '
  def accepted_lane($digest; $expected):
    [ .[] | select(.plan_digest == $digest) ] as $records |
    ($records | length) > 0 and
    ($records | last | .event) == "run.completed" and
    ($records | last | .payload.failed) == 0 and
    ($records | last | .payload.blocked) == 0 and
    (($records | last | .payload.succeeded) +
      ($records | last | .payload.resumed)) == $expected and
    ([ $records[] | select(.event == "recipe.succeeded") | .recipe ] |
      unique | length) == $expected and
    ([ $records[] | select(.event == "run.residency-inventoried") ] |
      last | .payload.installation_inventory_complete) == true and
    ([ $records[] | select(.event == "run.residency-inventoried") ] |
      last | .payload.all_feasible_installations_fit) == true;

  accepted_lane($spark3542; 29) and
  accepted_lane($spark2297; 30) and
  ([ .[] |
    select(.plan_digest == $spark3542 or .plan_digest == $spark2297) |
    select(.event == "recipe.succeeded") | .recipe ] |
    unique | length) == 59
' "$QUALIFICATION_ROOT/evidence/spark-3542.jsonl" \
  "$QUALIFICATION_ROOT/evidence/spark-2297.jsonl"
jq -e '.loaded | length == 0' \
  "$QUALIFICATION_ROOT/status/spark-3542.json" \
  "$QUALIFICATION_ROOT/status/spark-2297.json"
```

## Ordered dual-Spark follow-on lane

Run these one at a time, only after both single-Spark lanes have stopped their
runtimes. The order exercises the largest declared retained envelopes first and
the standard GLM profile before its gated abliterated variant. Each fresh
preview remains authoritative and may block safely if live capacity no longer
fits. Retained single-Spark installations and artifact caches stay in place.

1. `vonk-forge/glm-5-3-flash-nvfp4-vllm-dual`
2. `vonk-forge/glm-5-3-flash-nvfp4-kv-1m-abliterated-vllm-dual`
3. `vonk-forge/deepseek-v4-flash-0731-mia-dual`
4. `vonk-forge/inkling-small-nvfp4-sglang-dual`
5. `vonk-forge/qwen3-8-flash-next-nvfp4-sglang-dual`

Use one shared append-only ledger, but a distinct digest-bound preview/apply
pair for each recipe. Do not run the next pair until the previous apply ends in
`run.completed` and both warm-smoke runtimes have stopped.

```bash
uv run --frozen vonk-fleet-qualify --jurisdiction NL --cleanup stop \
  --operation-timeout-seconds 86400 --poll-interval-seconds 5 \
  --ledger "$QUALIFICATION_ROOT/dual/evidence.jsonl" \
  --recipe vonk-forge/glm-5-3-flash-nvfp4-vllm-dual \
  > "$QUALIFICATION_ROOT/dual/01-glm-standard-preview.json"
DUAL_PLAN_DIGEST="$(jq -er '.plan_digest' "$QUALIFICATION_ROOT/dual/01-glm-standard-preview.json")"
uv run --frozen vonk-fleet-qualify --jurisdiction NL --cleanup stop \
  --operation-timeout-seconds 86400 --poll-interval-seconds 5 \
  --ledger "$QUALIFICATION_ROOT/dual/evidence.jsonl" \
  --recipe vonk-forge/glm-5-3-flash-nvfp4-vllm-dual \
  --plan-digest "$DUAL_PLAN_DIGEST" --apply \
  > "$QUALIFICATION_ROOT/dual/01-glm-standard-apply.json"

uv run --frozen vonk-fleet-qualify --jurisdiction NL --cleanup stop \
  --operation-timeout-seconds 86400 --poll-interval-seconds 5 \
  --ledger "$QUALIFICATION_ROOT/dual/evidence.jsonl" \
  --recipe vonk-forge/glm-5-3-flash-nvfp4-kv-1m-abliterated-vllm-dual \
  > "$QUALIFICATION_ROOT/dual/02-glm-abliterated-preview.json"
DUAL_PLAN_DIGEST="$(jq -er '.plan_digest' "$QUALIFICATION_ROOT/dual/02-glm-abliterated-preview.json")"
uv run --frozen vonk-fleet-qualify --jurisdiction NL --cleanup stop \
  --operation-timeout-seconds 86400 --poll-interval-seconds 5 \
  --ledger "$QUALIFICATION_ROOT/dual/evidence.jsonl" \
  --recipe vonk-forge/glm-5-3-flash-nvfp4-kv-1m-abliterated-vllm-dual \
  --plan-digest "$DUAL_PLAN_DIGEST" --apply \
  > "$QUALIFICATION_ROOT/dual/02-glm-abliterated-apply.json"

uv run --frozen vonk-fleet-qualify --jurisdiction NL --cleanup stop \
  --operation-timeout-seconds 86400 --poll-interval-seconds 5 \
  --ledger "$QUALIFICATION_ROOT/dual/evidence.jsonl" \
  --recipe vonk-forge/deepseek-v4-flash-0731-mia-dual \
  > "$QUALIFICATION_ROOT/dual/03-deepseek-mia-preview.json"
DUAL_PLAN_DIGEST="$(jq -er '.plan_digest' "$QUALIFICATION_ROOT/dual/03-deepseek-mia-preview.json")"
uv run --frozen vonk-fleet-qualify --jurisdiction NL --cleanup stop \
  --operation-timeout-seconds 86400 --poll-interval-seconds 5 \
  --ledger "$QUALIFICATION_ROOT/dual/evidence.jsonl" \
  --recipe vonk-forge/deepseek-v4-flash-0731-mia-dual \
  --plan-digest "$DUAL_PLAN_DIGEST" --apply \
  > "$QUALIFICATION_ROOT/dual/03-deepseek-mia-apply.json"

uv run --frozen vonk-fleet-qualify --jurisdiction NL --cleanup stop \
  --operation-timeout-seconds 86400 --poll-interval-seconds 5 \
  --ledger "$QUALIFICATION_ROOT/dual/evidence.jsonl" \
  --recipe vonk-forge/inkling-small-nvfp4-sglang-dual \
  > "$QUALIFICATION_ROOT/dual/04-inkling-preview.json"
DUAL_PLAN_DIGEST="$(jq -er '.plan_digest' "$QUALIFICATION_ROOT/dual/04-inkling-preview.json")"
uv run --frozen vonk-fleet-qualify --jurisdiction NL --cleanup stop \
  --operation-timeout-seconds 86400 --poll-interval-seconds 5 \
  --ledger "$QUALIFICATION_ROOT/dual/evidence.jsonl" \
  --recipe vonk-forge/inkling-small-nvfp4-sglang-dual \
  --plan-digest "$DUAL_PLAN_DIGEST" --apply \
  > "$QUALIFICATION_ROOT/dual/04-inkling-apply.json"

uv run --frozen vonk-fleet-qualify --jurisdiction NL --cleanup stop \
  --operation-timeout-seconds 86400 --poll-interval-seconds 5 \
  --ledger "$QUALIFICATION_ROOT/dual/evidence.jsonl" \
  --recipe vonk-forge/qwen3-8-flash-next-nvfp4-sglang-dual \
  > "$QUALIFICATION_ROOT/dual/05-qwen-preview.json"
DUAL_PLAN_DIGEST="$(jq -er '.plan_digest' "$QUALIFICATION_ROOT/dual/05-qwen-preview.json")"
uv run --frozen vonk-fleet-qualify --jurisdiction NL --cleanup stop \
  --operation-timeout-seconds 86400 --poll-interval-seconds 5 \
  --ledger "$QUALIFICATION_ROOT/dual/evidence.jsonl" \
  --recipe vonk-forge/qwen3-8-flash-next-nvfp4-sglang-dual \
  --plan-digest "$DUAL_PLAN_DIGEST" --apply \
  > "$QUALIFICATION_ROOT/dual/05-qwen-apply.json"
```

Final read-only capture and dual acceptance check:

```bash
jq -s -e '
  ([.[] | select(.event == "run.completed")] | length) == 5 and
  ([.[] | select(.event == "recipe.succeeded") | .recipe] | unique | length) == 5 and
  ([.[] | select(.event == "run.residency-inventoried") |
    select(.payload.installation_inventory_complete == true)] | length) == 5
' "$QUALIFICATION_ROOT/dual/evidence.jsonl"
uv run --frozen vonkctl library list --all --json \
  > "$QUALIFICATION_ROOT/controller/library-final.json"
uv run --frozen vonkctl fleet show spk_2818d189042b4c77aefa7796f4befd23 --json \
  > "$QUALIFICATION_ROOT/controller/spark-3542-final.json"
uv run --frozen vonkctl fleet show spk_9a86fdbab116442ab6707bf4181a3c1c --json \
  > "$QUALIFICATION_ROOT/controller/spark-2297-final.json"
uv run --frozen vonkctl activity list --all --json \
  > "$QUALIFICATION_ROOT/controller/activity-final.json"
```

## Reviewed non-actionable catalog closure

These entries are evidence dispositions, not execution work. Do not bypass the
license or topology guards.

| Disposition | Recipe | Reason |
| --- | --- | --- |
| Legal block in NL | `vonk-forge/hunyuan3d-omni-pytorch-single` | Model license denies the operator jurisdiction. |
| Legal block in NL | `vonk-forge/hunyuanocr-1-5-vllm-dflash-single` | Model license denies NL/EU use. |
| Unsupported topology | `vonk-forge/glm-5-2-aqlm-vllm-triple` | Requires 3 Sparks. |
| Unsupported topology | `vonk-forge/glm-5-2-quanttrio-vllm-four` | Requires 4 Sparks. |
| Unsupported topology | `vonk-forge/glm-5-3-flash-nvfp4-vllm-four` | Requires 4 Sparks. |
| Unsupported topology | `vonk-forge/inkling-975b-a41b-nvfp4-sglang-eight` | Requires 8 Sparks. |

Together, the 59 single-Spark assignments, 5 dual-Spark follow-ons, 2 legal
blocks, and 4 topology blocks close all 70 catalog entries at the bound commit.

## Evidence acceptance fields

Preserve the raw JSON and JSONL artifacts. Acceptance must be traceable to:

- catalog repository, signed commit, catalog-index SHA-256, recipe URI,
  immutable content SHA-256, release version, and local revision ID;
- authority ID/SHA-256, campaign-manifest SHA-256, campaign digest, both lane
  plan digests, fixture-manifest SHA-256, jurisdiction, and `cleanup=stop`;
- controller authority revision, fleet fingerprint, event cursor, exact node
  IDs, online/readiness state, telemetry sample time/freshness, disk free/used,
  unified memory, reservations, and inter-node fabric for dual recipes;
- `capacity.plan.created`, execution order, assignments, roles/ranks, artifact
  providers and dedup, persistent/peak/projected bytes per node, safety floor,
  allocatable baseline, fit result, and `automatic_eviction: false`;
- every `step.previewed`, `operation.submitted`, and `operation.completed`
  record with step, request/plan digest, operation ID, owner ID, state, progress,
  and controller error or blocker codes;
- recipe/revision, mapping, build, installation, run/job, node, and alias IDs;
- exact fixture/capability case, bounded request and response/output digests,
  semantic validator results, service model alias/endpoint evidence or artifact
  output manifest, and all media/parser assertions;
- initial smoke, stop, warm redeploy of the same retained installation, second
  smoke, warm stop, `cleanup.retained`, and proof no second install occurred;
- `recipe.succeeded`, or the exact immutable/legal/topology/resource/operation
  blocker or failure without relabeling it as accepted;
- terminal `run.completed`, complete `run.residency-inventoried`, every retained
  installation/revision/node/state, dedup/reservation totals, final Library
  inventory, final fleet telemetry, and confirmation that no runtime is active.

Downloaded artifacts, installations, and caches may remain on both Sparks while
capacity fits. Do not uninstall or evict them merely to make the ledger look
clean; a later fresh capacity plan must explicitly report any shortfall.
