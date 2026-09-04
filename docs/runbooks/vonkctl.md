# Local controller CLI

`vonkctl` is the local command-line alternative to the browser controller. It
uses the same authenticated control API and persisted projections as the Fleet,
Library, and Activity pages. Read commands are safe by default. Every mutation
requires both an explicit `apply` subcommand where applicable and the `--apply`
flag.

## Connect to a controller

The CLI accepts only an HTTPS origin and reads its bearer credential from a
private, regular, non-symlink file. It never accepts a token on the command
line.

```bash
uv tool install .
chmod 600 .dev/admin-token
export VONK_CONTROL_URL=https://forge.example.test
export VONK_CONTROL_TOKEN_FILE="$PWD/.dev/admin-token"
vonkctl --help
```

During repository development, `uv run --frozen vonkctl ...` uses the checkout
directly instead. Add `--json` before or after the selected leaf command for
machine-readable output.

## Fleet

The Fleet command exposes the browser's search, health, warning, sort,
telemetry-range, profile, and Spark-enrollment choices.

```bash
vonkctl fleet list
vonkctl fleet list --search spark-2 --health stale --health offline --warnings-only
vonkctl fleet show spk_0123456789abcdef0123456789abcdef --json
vonkctl fleet telemetry spk_0123456789abcdef0123456789abcdef --range 24h --json
vonkctl fleet node-profile spk_0123456789abcdef0123456789abcdef --display-name "Studio Spark"
vonkctl fleet node-profile spk_0123456789abcdef0123456789abcdef --display-name "Studio Spark" --apply
vonkctl fleet agents --json
vonkctl fleet enrollments --state pending --json
vonkctl fleet enrollments --all --json
vonkctl fleet enroll
vonkctl fleet enroll --apply
vonkctl fleet re-enroll
vonkctl fleet re-enroll spk_0123456789abcdef0123456789abcdef --apply
vonkctl fleet revoke spk_0123456789abcdef0123456789abcdef --apply
vonkctl fleet upgrade candidate --json
vonkctl fleet upgrade preview --strategy one-at-a-time --json
vonkctl fleet upgrade apply --strategy one-at-a-time --plan-digest DIGEST --apply
```

`fleet telemetry --range` accepts the same `1h`, `24h`, `7d`, and `31d`
windows as the node cards. Explicit `--start`, `--end`, `--resolution`, and
`--maximum-points` options are available for scripts.
`fleet re-enroll` replaces a Spark certificate while preserving its identity;
pass a node ID to bind the grant or omit it for the web controller's generic
re-enrollment flow.

`fleet upgrade` rolls out the controller's current signed agent package over
the enrolled relay; it does not require SSH access to a Spark. Preview the exact
eligible nodes and package first, then apply that returned plan digest. The
default `one-at-a-time` strategy waits for each Spark to return healthy before
continuing. Repeat `--node-id SPARK_ID` to target a subset; omitting it selects
every eligible Spark.

`fleet current` shows observed workloads and placements, while `fleet state`
shows the current Spark roster, capacity, and freshness. The old
`fleet profile` spelling remains accepted as a compatibility alias for the
single-node display-name edit; saved whole-fleet profiles use the separate
`profiles` command group.

Spark metrics are returned by the Controller with their units, source,
freshness, support status, and aggregation metadata intact. Use the current
projection for hardware/runtime values, history for a bounded time series,
capabilities for support details, and workloads for run/engine metrics.

```bash
vonkctl fleet metrics current SPARK_ID --json
vonkctl fleet metrics history SPARK_ID --range 24h --json
vonkctl fleet metrics history SPARK_ID \
  --start 2026-09-05T00:00:00Z --end 2026-09-05T01:00:00Z \
  --resolution raw --maximum-points 100 --json
vonkctl fleet metrics capabilities SPARK_ID --json
vonkctl fleet metrics workloads SPARK_ID --run-id RUN_ID --state running --json
vonkctl fleet metrics export SPARK_ID --range 7d --file metrics.json --json
```

## Models, NAS cache, and profiles

The task-oriented groups keep model selection, NAS caching, and whole-fleet
switches explicit and machine-readable. Complex requests can be supplied as a
bounded JSON object with `--input JSON`, `--input-file FILE`, or `--stdin`.

```bash
vonkctl models discover --search qwen --all --json
vonkctl models show MODEL_ID --json
vonkctl models compare MODEL_ID MODEL_ID --json
vonkctl models run preview --input-file run.json --json
vonkctl models run apply --input-file run.json --plan-digest DIGEST \
  --request-key REQUEST_UUID --apply --json
vonkctl models run stop preview RUN_ID --json
vonkctl models run stop apply RUN_ID --plan-digest DIGEST \
  --request-key REQUEST_UUID --apply --json

vonkctl cache list --all --json
vonkctl cache show ARTIFACT_SET_SHA256 --json
vonkctl cache download preview --input-file exact-artifacts.json --json
vonkctl cache download apply --input-file exact-artifacts.json \
  --plan-digest DIGEST --request-key REQUEST_UUID --apply --json
vonkctl cache repair ARTIFACT_SET_SHA256 preview --json
vonkctl cache repair ARTIFACT_SET_SHA256 apply \
  --plan-digest DIGEST --request-key REQUEST_UUID --apply --json
vonkctl cache update ARTIFACT_SET_SHA256 --json
vonkctl cache eviction preview --target-bytes 1073741824 --json
vonkctl cache eviction apply --target-bytes 1073741824 \
  --plan-digest DIGEST --request-key REQUEST_UUID --apply --json

vonkctl profiles list --json
vonkctl profiles show PROFILE_ID --json
vonkctl profiles create --input-file profile.json --json
vonkctl profiles update PROFILE_ID --stdin --json
vonkctl profiles duplicate PROFILE_ID --name "Creative setup" \
  --request-key REQUEST_UUID --apply --json
vonkctl profiles capture-current --name "Current setup" \
  --request-key REQUEST_UUID --apply --json
vonkctl profiles preview PROFILE_ID --json
vonkctl profiles prepare preview PROFILE_ID --json
vonkctl profiles prepare apply PROFILE_ID \
  --plan-digest DIGEST --request-key REQUEST_UUID --apply --json
vonkctl profiles switch PROFILE_ID \
  --plan-digest DIGEST --request-key REQUEST_UUID --apply --json
vonkctl profiles status PROFILE_ID --json
vonkctl profiles delete PROFILE_ID --apply --json
```

Cache download requests identify an exact immutable artifact set; optional
`--model-version-sha256` and `--recipe-revision-sha256` flags are available for
the common single-selection form. Repair and eviction require the digest from
a fresh preview before `--apply`. Every consequential apply requires an
explicit reusable `--request-key`; reuse that exact key when rerunning an
apply after a lost connection.

`models --capability` filters advertised model support. Use
`models --recipe-capability` when the question is whether a recipe exposes a
capability; the CLI keeps these two sources separate.

## Operations and recovery

Operations return durable IDs that remain inspectable after the invoking
process exits. `wait` is bounded and never cancels server work on timeout;
`watch` performs one bounded observation and `evidence` retrieves the sanitized
diagnostic record.

```bash
vonkctl operations list --status running --all --json
vonkctl operations show OPERATION_ID --json
vonkctl operations wait OPERATION_ID --timeout-seconds 60 --json
vonkctl operations evidence OPERATION_ID --json
```

## Library and public catalog

Local Library browsing uses the same bounded server pagination. Public catalog
filter values match the browser, including model type, Spark count,
qualification, execution readiness, local status, capability, and sort order.

```bash
vonkctl library list --search qwen --all --json
vonkctl library show RECIPE_ID --json
vonkctl library compare RECIPE_ID RECIPE_ID --json
vonkctl library public list --model-type language --capability chat \
  --qualification cataloged --readiness executable --sort download --json
vonkctl library public facets --source-owner Qwen --json
vonkctl library public compare URI URI --json
vonkctl library public preview 'vonk://catalog/PUBLISHER/SLUG@sha256:DIGEST' --json
vonkctl library public import URI --expected-content-sha256 DIGEST
vonkctl library public import URI --expected-content-sha256 DIGEST --apply
```

Local and public comparisons accept two or three distinct recipes, matching the
browser comparison tray. `public facets` reports the available values and
counts after applying the other selected filters.

Custom recipe creation consumes the same canonical recipe document accepted by
the browser builder and server schema. `library template` emits the browser's
authoritative Custom, vLLM, or Diffusers starting document. Keeping the result
in a file makes a draft reviewable and reproducible.

```bash
vonkctl library template --preset vllm > recipe.json
vonkctl library template --preset diffusers --json
vonkctl library create --slug my-model --document recipe.json
vonkctl library create --slug my-model --document recipe.json --apply
vonkctl library update RECIPE_ID --expected-revision 3 --document recipe.json --apply
vonkctl library resolve RECIPE_ID --expected-revision 4 --apply
vonkctl library fork RECIPE_ID --revision 4 --slug my-model-experiment --apply
```

Operational changes retain the browser's preview/apply boundary:

```bash
vonkctl library map preview --recipe-revision-id REVISION_ID --node-id SPARK_ID --json
vonkctl library map apply --recipe-revision-id REVISION_ID --node-id SPARK_ID \
  --placement-digest DIGEST --apply

vonkctl library build preview --recipe-revision-id REVISION_ID \
  --builder-node-id SPARK_ID --json
vonkctl library build apply --recipe-revision-id REVISION_ID \
  --builder-node-id SPARK_ID --build-input-sha256 DIGEST --apply

vonkctl library distribute preview --recipe-build-id BUILD_ID \
  --mapping-id MAPPING_ID --mapping-generation GENERATION --json
vonkctl library distribute apply --recipe-build-id BUILD_ID \
  --mapping-id MAPPING_ID --mapping-generation GENERATION \
  --plan-digest DIGEST --apply

vonkctl library install preview --mapping-id MAPPING_ID --recipe-build-id BUILD_ID --json
vonkctl library install apply --mapping-id MAPPING_ID --recipe-build-id BUILD_ID \
  --plan-digest DIGEST --apply

vonkctl library load preview --installation-id INSTALLATION_ID --alias qwen-chat --json
vonkctl library load apply --installation-id INSTALLATION_ID --alias qwen-chat \
  --plan-digest DIGEST --apply

vonkctl library stop preview RUN_ID --json
vonkctl library stop apply RUN_ID --plan-digest DIGEST --apply
vonkctl library uninstall preview INSTALLATION_ID --json
vonkctl library uninstall apply INSTALLATION_ID --plan-digest DIGEST --apply
```

Apply commands generate an idempotency UUID automatically. Supply
`--request-key UUID` when a caller needs to retain and reuse it after an
uncertain outcome. The build apply command consumes `build_input_sha256` from
its preview; distribution apply consumes `plan_digest` from its preview and
must reuse the previewed mapping generation. Use `library operation show`,
`library operation retry`, and `library run` to follow progress and recovery.

## Artifact-producing recipe jobs

Image, image-editing, video, audio, mesh, and generic artifact recipes use the
bounded recipe-job protocol rather than an OpenAI service endpoint. Activate
the installed recipe with `library job activate`; this reserves its capacity
without trying to start a long-lived OpenAI service on the Spark. The apply
response's `owner_id` is the logical `RUN_ID` used by the remaining commands.

```bash
vonkctl library job capabilities --json
vonkctl library job activate preview \
  --installation-id INSTALLATION_ID --alias image-worker --json
vonkctl library job activate apply \
  --installation-id INSTALLATION_ID --alias image-worker \
  --plan-digest DIGEST --apply --json
```

`job capabilities` reports the controller's exact transfer ceilings, reserved
input names, and current artifact-store maximum, usage, and remaining bytes.
Apply-time `create` and `launch` fetch this preflight automatically and report
whether the distinct local input bytes fit without relying on content-addressed
blob reuse. The controller remains authoritative because an already stored
digest may allow a job even when raw remaining capacity is smaller.

Once the artifact recipe run is active, `library job launch` performs the
complete create, input upload, finalize, and submit sequence. It remains a dry
run until `--apply` is explicit:

```bash
vonkctl library job launch RUN_ID \
  --interface image-job \
  --parameters parameters.json \
  --input prompt prompt.txt text/plain ./prompt.txt \
  --input source source.png image/png ./source.png \
  --output-media-type image/png \
  --max-output-files 1 \
  --max-output-file-bytes 1073741824 \
  --max-output-total-bytes 1073741824 \
  --timeout-seconds 3600 --json

vonkctl library job launch RUN_ID \
  --interface image-job \
  --input prompt prompt.txt text/plain ./prompt.txt \
  --output-media-type image/png \
  --apply --json
```

`create` and `launch` generate an idempotency UUID for draft creation. Supply
`--request-key UUID` and retain it when a caller needs to retry after an
uncertain create response without duplicating the job.

Inputs are declared from local regular, non-symlink files. The CLI streams each
file to calculate its exact SHA-256 and size, then rechecks that identity during
upload. Repeat `--input SLOT NAME MEDIA_TYPE PATH` for up to 32 inputs. `SLOT`
maps the file to the recipe contract's named input (for example, `prompt`,
`source`, or `audio`). Each input is limited to 512 MiB and the combined input
manifest to 1 GiB. The controller allows at most 32 outputs, 1 GiB per output,
and 2 GiB total. MIME types must be lowercase and explicit; the CLI does not
guess them from extensions.

Each stage is also available independently, so an interrupted launch can be
inspected and resumed without creating another job:

```bash
vonkctl library job create RUN_ID --interface video-job \
  --input prompt prompt.txt text/plain ./prompt.txt \
  --output-media-type video/mp4 --apply --json
vonkctl library job upload JOB_ID \
  --input prompt prompt.txt text/plain ./prompt.txt --apply --json
vonkctl library job finalize JOB_ID --apply --json
vonkctl library job submit JOB_ID --apply --json
vonkctl library job list RUN_ID --json
vonkctl library job status JOB_ID --json
vonkctl library job result JOB_ID --json
vonkctl library job cancel JOB_ID --reason "operator requested" --apply --json
```

Result downloads always begin by loading the controller's successful result
manifest. A dry run shows the exact names, sizes, media types, digests, and
destinations. `--apply` streams every selected file into a private temporary
file in the destination directory, verifies its size, MIME type, response
digest, and content SHA-256, then publishes it atomically. Existing files are
not replaced unless `--overwrite` is explicit.

```bash
mkdir -p ./outputs
vonkctl library job download JOB_ID --output-directory ./outputs --json
vonkctl library job download JOB_ID --output-directory ./outputs --apply --json
vonkctl library job download JOB_ID --output-directory ./outputs \
  --sha256 DIGEST --overwrite --apply --json
```

## Activity

```bash
vonkctl activity list --search qwen --area Library --operator admin \
  --status unsuccessful --sort attention --all --json
vonkctl activity jobs --status running --limit 50 --all --json
vonkctl activity job JOB_ID --json
vonkctl activity resume JOB_ID
vonkctl activity resume JOB_ID --apply
```

The Activity list combines the same audit and job records as the browser,
including best-effort Fleet and Library target names. Its search, area,
operator, status, and recent/attention sort choices match the web view. Use
`--all` to follow continuation cursors up to the CLI's cycle-safe page bound;
explicit cursor options remain available for bounded automation.

## Failure behavior

The CLI rejects redirects, non-HTTPS controller origins, credentials embedded
in URLs, unsafe token files, oversized responses, and malformed JSON. Remote
error text is bounded and secrets are redacted. Exit status `0` means the
command completed or printed a dry-run plan; exit status `2` means arguments,
input, authentication, transport, or the control API failed.
