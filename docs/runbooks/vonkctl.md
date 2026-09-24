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
`vonkctl --version` reports the installed build without Controller credentials.
The [accepted CLI update command](../operators/cli-updates.md) checks the signed
installer publication and applies an update only when requested.

When using the browser, sign in to the Controller and open the operator menu
under your user name. Choose **Download CLI token** (it appears before
Activity). The browser downloads a `vonkctl-token` file without rendering the
credential in the page. Store that file privately, then point the CLI at it:

```bash
export VONK_CONTROL_URL=https://forge.example.test
install -d -m 700 "$HOME/.config/vonk-forge"
mv "$HOME/Downloads/vonkctl-token" "$HOME/.config/vonk-forge/controller-token"
export VONK_CONTROL_TOKEN_FILE="$HOME/.config/vonk-forge/controller-token"
chmod 600 "$VONK_CONTROL_TOKEN_FILE"
vonkctl model library --json
```

For persistent configuration on macOS with zsh, put the two `export` lines in
`~/.zshenv`. They will then be available in new interactive and non-interactive
zsh sessions, independently of the current directory. Store only the token's
file path in shell configuration; keep the credential in the private file above.
Programs launched without a shell need the same environment variables in their
own launch configuration. The CLI currently has no separate connection config file.

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

Recipe names (`publisher/slug` or an unambiguous slug) and logical recipe IDs
select the current accepted revision. Use an exact revision ID or content digest
to address a retained revision explicitly.

Use the same `--request-key UUID` when repeating a download after a lost
response. It returns the accepted operation, including a requested rebuild.
Controller worker recovery also rejoins that attempt's existing build instead
of starting it again. A new explicit download can still refresh a completed
image.

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
vonkctl --profile 2 profile add "Qwen Code" --spark Atlas --spark Boreal
vonkctl --profile 2 profile remove "Qwen Code" --spark Boreal
vonkctl --profile 2 profile load --dry-run
vonkctl --profile 2 profile load
vonkctl --profile 2 profile load --detach
vonkctl --profile 2 profile progress --follow
```

Profile authoring stores the canonical recipe identity (`publisher/slug`), sorted
Spark IDs, optional assignment name, optional exact model variant, and desired
state. The CLI resolves an exact recipe title, slug, or canonical selector
before saving; the web editor likewise sends the selected library row's
canonical selector. It does not pin a recipe revision or declare a subset scope.
The Controller returns warnings for incomplete groups and resource pressure at
save time. A load preview reports blockers, resolved immutable identities, the
whole-fleet snapshot, resource fit, and a `plan_digest`. The CLI submits that
digest with the load request, and the Controller rejects the request if the
preview no longer describes the current plan.

### Sequential recipe qualification

`vonk-fleet-qualify-campaign` executes one reviewed authority row at a time.
Use the campaign manifest and recipe checkout named by the current qualification
plan, plus a durable ledger outside that checkout. Reserve a dedicated profile
with `installation_policy=keep-cached` and labels
`qualification-authority=<authority ID>` and
`qualification-ledger=<runner ledger identity>`. The ledger identity is the
first 63 hexadecimal characters of the SHA-256 of its resolved absolute path.

Preview requires `--manifest`, `--library-root`, `--ledger`, `--profile-number`,
`--recipe`, and the exact Controller `--spark` IDs for that row. It saves the
recipe assignment in this dedicated profile and records the reviewed preview;
it does not load the profile. Every profile covers the full enrolled fleet,
including Sparks left idle. A dual-Spark row also requires `--failure-spark`
to identify its later recovery checkpoint.

Prepare missing NAS assets through `vonkctl recipe download` before preview.
Spark-local copies may be cold: the accepted plan owns their transfer. If a
healthy workload already occupies the fleet, both preview and apply require
`--replace-run-id` naming that exact run. Its observed membership and planned
interruption become part of the reviewed campaign digest.

To execute, repeat the reviewed selection with `--apply --campaign-digest DIGEST`.
Add `--accept-operator-gate RECIPE` and `--accept-capacity-review RECIPE` only for
the gates declared by that row. Preview provides the evidence for these apply
acknowledgements. A changed plan or run requires a fresh preview.

Use `--observe` with the same manifest, library, ledger, profile and recipe to
reconcile an interrupted submission or advance a recorded physical checkpoint.
Observe takes no Spark selection or acknowledgement flags. Follow its reported
checkpoint instructions for rank loss and host restart; those physical actions
are operator-run. A passing initial smoke is not complete physical acceptance.
Cleanup stops the campaign workload and retains verified cached assets.

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
A new profile load reconciles current fleet/cache state and supersedes older
overlapping requests. Reusing its request key returns the same durable
operation. Observation can be interrupted without cancelling accepted work;
use `profile progress --follow` to reconnect to it.
