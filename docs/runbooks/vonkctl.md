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
vonkctl fleet profile spk_0123456789abcdef0123456789abcdef --display-name "Studio Spark"
vonkctl fleet profile spk_0123456789abcdef0123456789abcdef --display-name "Studio Spark" --apply
vonkctl fleet agents --json
vonkctl fleet enrollments --state pending --json
vonkctl fleet enrollments --all --json
vonkctl fleet enroll
vonkctl fleet enroll --apply
vonkctl fleet re-enroll
vonkctl fleet re-enroll spk_0123456789abcdef0123456789abcdef --apply
vonkctl fleet revoke spk_0123456789abcdef0123456789abcdef --apply
```

`fleet telemetry --range` accepts the same `1h`, `24h`, `7d`, and `31d`
windows as the node cards. Explicit `--start`, `--end`, `--resolution`, and
`--maximum-points` options are available for scripts.
`fleet re-enroll` replaces a Spark certificate while preserving its identity;
pass a node ID to bind the grant or omit it for the web controller's generic
re-enrollment flow.

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
uncertain outcome. Use `library operation show`, `library operation retry`, and
`library run` to follow progress and recovery.

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
