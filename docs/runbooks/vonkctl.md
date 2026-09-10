# `vonkctl` operator CLI

`vonkctl` is the local, authenticated CLI for the Controller. It has four
singular operator areas: Fleet, Model, Recipe, and Profile. The CLI uses the
same current Controller routes as the operator API: `/api/fleet`,
`/api/model`, `/api/recipe`, and `/api/profile`. Machine-facing agent
transport is separate and is not a CLI fallback.

## Connect

The CLI accepts an HTTPS Controller origin and reads its bearer credential from
a private regular file. It never accepts a token on the command line.

```bash
uv tool install .
export VONK_CONTROL_URL=https://forge.example.test
export VONK_CONTROL_TOKEN_FILE="$PWD/.dev/admin-token"
chmod 600 "$VONK_CONTROL_TOKEN_FILE"
vonkctl --help
```

Use `--json` for one parseable object on stdout. Use `--profile N` to select a
stable numbered Profile for that invocation; the default is Profile 1.

When using the browser, sign in to the Controller and open the operator menu
under your user name. Choose **Download CLI token** (it appears before
Activity). The browser downloads a `vonkctl-token` file without rendering the
credential in the page. Store that file privately, then point the CLI at it:

```bash
export VONK_CONTROL_URL=https://forge.example.test
export VONK_CONTROL_TOKEN_FILE="$HOME/Downloads/vonkctl-token"
chmod 600 "$VONK_CONTROL_TOKEN_FILE"
vonkctl models discover --all --json
```

The download requires the active browser session and its CSRF protection. The
administrator bearer token expires after 30 days; download a new file from the
operator menu when it expires.

## Fleet

```bash
vonkctl fleet
vonkctl fleet --health stale --health offline --warnings-only
vonkctl fleet detail Atlas --metrics all --range 24h
vonkctl fleet detail Atlas --watch --interval-seconds 2
vonkctl fleet rename Atlas "Studio Spark"
vonkctl fleet enroll Atlas
vonkctl fleet re-enroll Atlas
vonkctl fleet remove Atlas --yes
vonkctl fleet upgrade Atlas
vonkctl fleet upgrade --all
vonkctl fleet loginfo Atlas --since 15m --lines 100 --follow
```

Friendly Spark names and exact stable selectors are accepted. Mutations are
single-step Controller operations; asynchronous work is followed to a terminal
response by default. Use `--detach` on an asynchronous Model or Recipe
mutation to return the accepted operation immediately. `--yes` is required for
removals; there is no obsolete `fleet profile` alias. `detail` retains
stale/unsupported measurements as explicit states, and `--watch` repaints
bounded observations until a terminal state or timeout. `loginfo` reads
bounded sanitized logs through the authenticated Controller path and never
falls back to SSH.

## Model

```bash
vonkctl model
vonkctl model library --family Qwen --usage code --sort updated
vonkctl model detail qwen-3.8-nvfp4
vonkctl model detail qwen-3.8-nvfp4 --watch
vonkctl model download qwen-3.8-nvfp4
vonkctl model download qwen-3.8-nvfp4 --detach
vonkctl model download qwen-3.8-nvfp4   # repeat to refresh a completed copy
vonkctl model remove qwen-3.8-nvfp4 --yes
```

The overview combines downloading, cached, and running variants. Download
repeats attach to active work, resume incomplete work, and refresh completed
work when requested. Removing a model cancels Controller preparation and
removes its Controller cache while preserving Profile assignments and
Spark-local running copies.

## Recipe

```bash
vonkctl recipe
vonkctl recipe library
vonkctl recipe library --model "Qwen 3.8" --all-models
vonkctl recipe detail qwen-code
vonkctl recipe detail qwen-code --watch
vonkctl recipe download qwen-code       # repeat to refresh a completed copy
vonkctl recipe download qwen-code --detach
vonkctl recipe update
vonkctl recipe update --all
vonkctl recipe remove qwen-code --keep-model --yes
vonkctl recipe remove qwen-code --with-model --yes
```

Recipe library defaults to exact model variants that are cached, downloading,
or running locally. `--all-models` broadens that view. Recipe removal keeps
the model cache by default; choose `--keep-model` explicitly to preserve it or
`--with-model` to request dependent model removal. A removal without an
explicit choice fails closed rather than prompting or guessing. Cached updates
are automatically used by the next Profile load; there is no Profile update
command.

## Profile

Profiles cover the entire enrolled fleet. Editing autosaves a permissive draft;
it does not stop or restart workloads. Every Spark remains visible and an
unassigned Spark is explicitly idle. Load performs strict complete-topology
validation before changing any workload and resolves the latest verified
cached compatible recipe/model.

```bash
vonkctl profile
vonkctl profile list
vonkctl --profile 2 profile name "Coding"
vonkctl --profile 2 profile add qwen-code --spark Atlas --spark Boreal
vonkctl --profile 2 profile remove qwen-code --spark Boreal
vonkctl --profile 2 profile load --dry-run
vonkctl --profile 2 profile load
vonkctl --profile 2 profile load --detach
vonkctl --profile 2 profile progress --follow
```

Profile authoring stores logical recipe identity (`recipe_selector`), sorted Spark identity
(`spark_ids`), optional assignment name, optional exact model variant, and
desired state. It does not pin a recipe revision or declare a subset scope.
The Controller returns warnings for incomplete groups and resource pressure at
save time. A load preview reports blockers, resolved immutable identities, the
whole-fleet snapshot, resource fit, and the plan it will bind internally.

## Output and recovery

Human output is an adaptive plain terminal table. At narrow widths the renderer
keeps identity, state, and the next action first; redirected output contains no
cursor control. `--wide` requests full columns. `--json` keeps progress and
errors machine-readable, including `operation_id`, `state`, `phase`, byte
counts, warnings, and next actions when supplied by the Controller. Completed
operations exit 0, partial operations exit 1, and failed, blocked, cancelled,
or timed-out operations exit 2.

Unknown totals are shown as unknown; a build step count is not converted into a
fake percentage. A failed refresh preserves the last verified cache copy.
Repeating a profile load reconciles current fleet/cache state and follows the
durable operation when the Controller reports one. Observation can be
interrupted without cancelling accepted work.
