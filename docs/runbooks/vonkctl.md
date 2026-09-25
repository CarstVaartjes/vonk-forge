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
Profile edits and loads require an explicit `--profile N`; there is no saved
selection. Running `vonkctl` without a command shows offline orientation.
`vonkctl --version` reports the installed build without Controller credentials.
The [accepted CLI update command](../operators/cli-updates.md) checks the signed
installer publication and applies an update only when requested.

Human output shows current state, complete identifiers, blockers, and the next
available action. Narrow terminals use stacked records; tables are used when
their values fit. `--wide` includes more resource or node detail. Unknown
measurements are labelled unavailable, and zero remains zero. Lists disclose
whether another page exists; retain the same filters when continuing its cursor.

Results go to stdout. Warnings, failure details, prompts, and ongoing observation
go to stderr. Resource watches append updated snapshots and keep the final
snapshot on stdout. Machine output preserves complete validated values rather
than terminal abbreviations. Color is not needed to understand any state;
control characters are escaped in human text, and ASCII terminals receive
escaped names when their encoding cannot represent them.

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

`vonkctl --check-connection --json` validates the origin and private regular
token file, then makes one authenticated Fleet read. It reports the actual
failed boundary without exposing credentials. Help, version, and completion
generation work without credentials or a network connection.

Generate completion with `vonkctl completion bash` or `vonkctl completion zsh`.
Source the generated script from your shell configuration after inspecting it;
Zsh requires its normal `compinit` setup. Generation never edits shell files or
queries the Controller.

## Fleet

```bash
vonkctl fleet
vonkctl fleet --health stale --health offline --warnings-only
vonkctl fleet detail Atlas --metrics all --range 24h
vonkctl fleet detail Atlas --watch --interval-seconds 2
vonkctl fleet node-profile Atlas
vonkctl fleet rename Atlas "Studio Spark"
vonkctl fleet enroll Atlas --output atlas-grant.json
vonkctl fleet re-enroll Atlas --output atlas-replacement.json --yes
vonkctl fleet enrollment status GRANT_UUID
vonkctl fleet enrollment revoke GRANT_UUID --yes
vonkctl fleet remove Atlas --yes
vonkctl fleet upgrade Atlas --yes
vonkctl fleet upgrade --all --yes
vonkctl fleet loginfo Atlas --since 15m --lines 100 --follow
vonkctl fleet progress JOB_ID --follow
vonkctl fleet activity --state waiting-for-operator --limit 20
vonkctl fleet resume JOB_ID --yes
```

Friendly Spark names and exact stable selectors are accepted. Mutations are
single-step Controller operations. Asynchronous Model/Recipe mutations and
Profile loads follow to a terminal response by default. Use `fleet progress`
to inspect or follow the job identity returned by a fleet upgrade.
Use `--detach` on an asynchronous Model or Recipe
mutation to return the accepted operation immediately. `--yes` is required for
Model and Recipe cache removals. `fleet remove` resolves and displays the
stable Spark ID before interactive confirmation; JSON, redirected input, or
`--no-input` requires `--yes`. There is no obsolete `fleet profile` alias.
`detail` retains stale/unsupported measurements as explicit states, and
`--watch` reports
bounded observations until a terminal state or timeout. `loginfo` reads
bounded sanitized logs through the authenticated Controller path and never
falls back to SSH. It resolves a friendly name once, then follows and reconnects
using that Spark's stable ID. A response identifying another Spark is refused.
Fleet upgrades require consent after the selected scope is identified. Interactive
use asks once; scripts, JSON output, redirected input, and `--no-input` must pass
`--yes`. A friendly single-Spark selector is resolved to its stable node ID
before submission. `--all` keeps the Controller's all-current-Sparks request
intent, and the accepted receipt binds the exact target list and plan.
When the Controller returns retained logs with no live follow support, the CLI
returns that snapshot immediately instead of waiting for a local timeout.

`fleet node-profile` resolves an exact enrolled node and shows its identity,
hostname, lifecycle, and labels. `--json` retains the full canonical Fleet
detail document. It never edits or loads a workload profile. Exact node IDs
take priority over displayed names; duplicate names return every candidate ID.

Profile recipe selection reads all catalog pages before accepting a friendly
name. Canonical recipe selectors and logical recipe IDs take priority over
titles, and prefixes are refused. `profile add`, `profile remove`, and
`fleet node-profile` share a selection deadline across their reads: 30 seconds
by default, configurable with `--timeout-seconds` up to 300. Expiry, repeated
cursors, malformed rows, and ambiguous choices save nothing. If a catalog
continuation becomes invalid because its matching collection changed, repeat
the command; for an explicit list page, restart without `--cursor`.

The Fleet overview counts each run once by its immutable run ID and groups its
member Sparks together. Controller run state, observed group health, rank state,
freshness, and route state stay distinct. `--wide` also groups installed
placements by installation ID. A filtered view names reported members outside
the selection; it does not treat omitted members as healthy or absent. Saved
desired assignments remain visible through the Profile view.

Enrollment and re-enrollment require a new `--output FILE`. The CLI reserves
that private file before asking the Controller for a grant. The grant document
and its one-use token are written only to that file; terminal and JSON results
contain a nonsecret receipt. Use the document's values with the
[Spark onboarding flow](node-onboarding.md). Re-enrollment first resolves the
exact Spark identity and confirms that the grant can authorize its replacement.
Scripts and `--no-input` require `--yes`; interactive use asks once.

The CLI generates a request UUID before issuance; `--request-key UUID` selects
one explicitly. This UUID is also the grant identity. Reusing it never mints or
redelivers a token. If the response is lost, the CLI queries that original grant
and reports its status. The reserved file also holds a nonsecret recovery
receipt until delivery succeeds. Do not treat a failed or interrupted delivery
as permission to create another grant automatically.

`fleet enrollment status` distinguishes pending, expired, consumed, and revoked
grants. Status and revocation require the issuing administrator's account.
After a confirmed issuance followed by a file-write failure, the CLI attempts
to revoke that still-unused grant and reports whether reconciliation succeeded.
For uncertain issuance, inspect the printed identity and explicitly revoke an
unused grant before starting again with a fresh request and output path.
Revocation cannot undo a consumed grant or revoke the resulting certificate;
use the enrolled Spark's management flow for those effects. Existing paths,
symlinks, denied authority, expired grants, and consumed grants remain explicit
failures or terminal states, never reasons to bypass enrollment checks.

Activity lists durable operations with their complete identities and current
owner-provided actions. Keep the same filters when continuing with `--cursor`;
`--target` takes an exact Spark ID and `--request-id` takes the original request
UUID. An unreadable historical row is labelled unavailable instead of hiding
unrelated operations. Inspect its exact operation for the detailed failure.
Pending profile cancellation appears as `cancelling`, with its cancellation
request key, actor, and completed, pending, or unissued effects. Filtering Activity
by `--state cancelling` uses that same owner state.

`fleet resume JOB_ID --yes` first checks the owner's advertised resume action.
The Controller rechecks current authorization and the intent of every target
before accepting it. Resume does not grant permission to replay superseded or
revoked work. Already authorized retries retain their original retry budget.

Fleet upgrades use `--strategy one-at-a-time`; this is the only accepted
strategy. The receipt identifies the exact job to follow. A failure stops
consequential rollout to later Sparks. This command upgrades enrolled Sparks;
Controller deployment and the signed local CLI `update` command have separate
owners.

## Model

```bash
vonkctl model
vonkctl model library --family Qwen --usage code --sort updated
vonkctl model detail qwen-3.8-nvfp4
vonkctl model detail qwen-3.8-nvfp4 --watch
vonkctl model download qwen-3.8-nvfp4
vonkctl model download qwen-3.8-nvfp4 --detach
vonkctl model progress OPERATION_ID --follow
vonkctl model progress --request-key REQUEST_UUID --follow
vonkctl model cancel OPERATION_UUID --yes --request-key CANCEL_UUID --reason "No longer needed"
vonkctl model download qwen-3.8-nvfp4   # repeat to refresh a completed copy
vonkctl model remove qwen-3.8-nvfp4 --review
vonkctl model remove qwen-3.8-nvfp4
```

The overview combines downloading, cached, and running variants. Reusing the
same download request key returns its original operation. A new request has
its own identity; the cache owner reuses verified assets and compatible partial
work and refreshes completed work when requested.

Cancel an exact download with `model cancel`; use `--detach` to return after
acceptance. The cancellation has its own request UUID and reason. Retain both
for recovery after response or process loss. The same cancellation request is
idempotent; a different request cannot replace it. Cancellation may remain
`cancelling` while an issued writer settles. Verified assets and compatible
partial files are retained; cancellation does not evict them. Reconnect with
`model progress OPERATION_ID --follow` to observe settlement.

Removal first presents the Controller-owned review: exact assets, their
verified/partial/missing/unknown readiness and byte counts, saved references,
active work, and blockers. `model remove SELECTOR --review` is read-only and
prints the review digest for scripts. In a terminal, `model remove SELECTOR`
shows the same impact before asking for consent. A scripted request must pass
the exact digest and explicit consent:

```bash
REVIEW=$(vonkctl --json model remove qwen-3.8-nvfp4 --review)
REVIEWED_DIGEST=$(printf '%s' "$REVIEW" | jq -r .review_digest)
vonkctl --json model remove qwen-3.8-nvfp4 \
  --review-digest "$REVIEWED_DIGEST" --yes --request-key REQUEST_UUID --detach
```

The Controller rechecks the same digest at acceptance. If the reviewed impact
changed, the CLI refuses without retrying against the new review; inspect it and
make a new explicit decision. Accepted references can block removal, and an
unavailable reference scan is an explicit refusal. Removal does not cancel
another download or build, stop a Spark workload, or erase a saved profile.

Keep the request key printed by the CLI. Reuse it to reconnect after a lost
response; `model progress --request-key REQUEST_UUID --follow` observes the
original removal. Queued and partially completed removals remain unfinished.
A storage or ownership conflict exposes its cause and next automatic retry;
progress resumes from the original checkpoint when that dependency clears.
A timeout or Ctrl-C ends observation without cancelling removal.

## Recipe

```bash
vonkctl recipe
vonkctl recipe library
vonkctl recipe library --model "Qwen 3.8" --all-models
vonkctl recipe library --all-models --fits-fleet
vonkctl recipe library --all-models --ready
vonkctl recipe detail qwen-code
vonkctl recipe detail qwen-code --watch
vonkctl recipe download qwen-code       # repeat to refresh a completed copy
vonkctl recipe download qwen-code --detach
vonkctl recipe progress OPERATION_ID --follow
vonkctl recipe progress --request-key REQUEST_UUID --follow
vonkctl recipe cancel OPERATION_UUID --yes --request-key CANCEL_UUID --reason "No longer needed"
vonkctl recipe update publisher/recipe
vonkctl recipe update --all
vonkctl recipe remove qwen-code --keep-model --review
vonkctl recipe remove qwen-code --with-model --review
vonkctl recipe remove qwen-code --keep-model
vonkctl recipe remove qwen-code --with-model
vonkctl recipe installation reconcile INSTALLATION_UUID --review
```

Recipe removal requires an explicit `--keep-model` or `--with-model` choice.
The read-only `--review` form reports the exact recipe revision and affected
archives/model assets, readiness, references, active work, blockers, and digest.
In a terminal, the removal command shows that impact before asking for consent.
For a scripted request, first capture the review and pass its exact digest with
`--yes`:

```bash
REVIEW=$(vonkctl --json recipe remove qwen-code --keep-model --review)
REVIEWED_DIGEST=$(printf '%s' "$REVIEW" | jq -r .review_digest)
vonkctl --json recipe remove qwen-code --keep-model \
  --review-digest "$REVIEWED_DIGEST" --yes --request-key REQUEST_UUID --detach
```

The Controller rechecks the review digest before accepting removal. A changed
review is refused and is never automatically resubmitted. `--with-model` binds
the exact reviewed model-removal child and completes only after that child
settles. Shared model files needed by retained cache entries are preserved.
Use `recipe progress --request-key REQUEST_UUID --follow` to reconnect to the
same operation and inspect a waiting dependency or failure.

Installation reconciliation targets one exact stopped installation whose
stored identity or installed-node state is invalid. It checks the original
successful install receipts and previews the current per-node cleanup effects;
it does not select a recipe by name or remove Controller cache assets. Review
the plan, then pass its digest and a retained request UUID to accept it:

```bash
REVIEW=$(vonkctl --json recipe installation reconcile INSTALLATION_UUID --review)
REVIEWED_DIGEST=$(printf '%s' "$REVIEW" | jq -r .plan_digest)
vonkctl --json recipe installation reconcile INSTALLATION_UUID \
  --review-digest "$REVIEWED_DIGEST" --yes --request-key REQUEST_UUID --detach
```

The Controller rechecks the exact installation identity and plan digest before
accepting cleanup. The same request UUID reconnects to an accepted operation,
including after a lost response; a failed lookup does not authorize a new
request. Follow the typed Run/Switch operation receipt with the returned
operation ID or rerun the same command with its original request UUID.

Recipe names (`publisher/slug` or an unambiguous slug) and logical recipe IDs
select the current accepted revision. Use an exact revision ID or content digest
to address a retained revision explicitly.

Recipe reads distinguish current fleet fit, exact NAS assets, and readiness.
`--fits-fleet` requires a complete placement that fits fresh current capacity;
it does not assume that existing workloads will stop. `--ready` also requires
the exact model files, companion dependencies, and authorized image archive on
the NAS, plus an admissible run plan. Spark copies may still need preparation.
These observations do not authorize a load or prove physical model quality.

The Controller uses its existing placement planner and cache owners for these
assessments. Inspection cannot create a planned build or dispatch work. A
recipe can fit while its cache is blocked; use the reported prepare action.
Unknown evidence stays unavailable. Filtering requires a known result for
every otherwise matching candidate, so unavailable evidence returns an explicit
error rather than a misleading empty page. Narrow the ordinary filters or
remove the readiness filter to inspect the reasons.

Ordinary lists assess their displayed page; readiness filters assess matching
candidates before pagination. Assessment has a shared five-second computation
budget. Results arriving after the budget are discarded, and unexamined groups
are unavailable, never treated as non-fitting. JSON retains the observation
time, candidate group, capacity evidence, and named reasons. The API's
`assess=false` option skips assessment for exact identity scans; it cannot be
combined with readiness filters.

Library `--limit` is a requested maximum. A page can contain fewer entries to
keep the complete JSON response within the 1 MiB document limit. Continue with
the returned cursor and the same limit and filters; a short page does not mean
the collection is exhausted.

After a lost download response, the CLI first looks up the original request
key. A matching receipt resumes following that operation. Only an authoritative
not-found permits one replay of the identical request; failed or denied lookup
does not. Malformed replies allow read-only diagnosis, and a received 401/403
stops submission even if its body was lost. A second lost response exits with
acceptance explicitly unknown and the same reconnect command.

Manual reconnection remains available with
`vonkctl model progress --request-key UUID --follow` or the equivalent
`recipe progress` command. Lookup resolves the key once and follows that exact
operation, even if newer work starts. Omit `--follow` for a single read. Supply
and retain `--request-key UUID` before invoking a download when recovery must
survive unconditional CLI process death. Human output flushes the request key
and reconnect command before submission. JSON emits only its final result or
structured error; an unconditionally killed process cannot emit that document.

Submission schedules at most three calls against one deadline, derived from
the client's request timeout (normally three calls × 15 seconds). Each call
receives the smaller of its normal timeout and the remaining budget. Server
retry delays consume that budget and are never shortened to retry early.
Following has its separate `--timeout-seconds` observation budget. Network
requests enforce an elapsed-time deadline across connection, headers, and body
reads; slow continuous traffic cannot extend it. Expiry or Ctrl-C closes the
connection without cancelling remote work. A stalled system DNS lookup cannot
hold the CLI process open or send a request after its caller has timed out.

Repeating the same download with the same key and original selector/options
returns its accepted operation before consulting changed catalog heads or
current download admission. A changed issuer or intent is refused. Request-key
lookup is limited to the original issuer with current read access; reads by
operation ID retain shared authenticated visibility. Read access alone never
permits a repeated POST. The original action and selector must also match a
recovered receipt; a different operation cannot be adopted as successful
submission.

Recipe acceptance records the parent before its worker can prepare model
assets. The first receipt can therefore be queued without a model-child ID;
following exposes the child after the worker creates it. Controller recovery
rejoins the attempt's existing build instead of starting it again. A new
explicit download can still refresh a completed image.

`recipe update SELECTOR` or `recipe update --all` accepts one durable update
operation and follows it by default. `--detach` returns its receipt immediately.
The receipt contains the original request key, exact recipe revisions, child
request keys, and observed child outcomes. Reconnect with `recipe progress
--request-key KEY --follow` or the returned operation ID. A lost acceptance
response uses the same bounded lookup/replay policy as a download.

`--all` selects every logical recipe with a verified, authorized image in the
managed Controller cache, then freezes its current accepted revision. Successful
job history alone does not establish cache presence. An empty cache is an
explicit successful no-op; replay retains that original empty scope. A scope
that cannot fit the complete document, including future failure observations,
is refused before child work starts, with the required bytes and limit.
It is never silently truncated to the first 100 recipes.

Worker restart reconciles saved child request keys before creating missing
work. Newly revoked authority prevents new child admission; already-issued
work remains observable. Each failure stays attached to its recipe while
other children continue. Activity shows the parent; `recipe progress` shows
its complete child detail. `recipe cancel OPERATION_ID --yes` targets this exact
preparation or update parent; retain its cancellation request key and reason.
Shared children needed by another accepted request must survive cancellation.
The command follows by default, or returns the accepted receipt with `--detach`.
When preparation owns a model download, cancellation waits for that exact
child and any active writer to settle. Restart retains the cancellation intent
and compatible partial work. An accepted cancellation does not prove every
issued effect has settled.

A model download waiting for another cache writer reports
`model_cache.object_busy`, the exact object, and its next retry time. Image
export contention reports `runtime_image.transfer_contended`. These waits
release execution capacity so unrelated eligible work can continue, and they
do not consume the failed-transfer retry allowance. Leave the original request
running and reconnect to it; a new request is not required when the writer
releases its lock. Compatible partial model files survive worker termination
and are resumed from their retained length before verification and publication.

Recipe library defaults to exact model variants that are cached, downloading,
or running locally. `--all-models` broadens that view. Recipe removal requires an explicit model-cache choice: use `--keep-model` to preserve it or
`--with-model` to request dependent model removal. A removal without an
explicit choice fails closed rather than prompting or guessing. Cached updates
are automatically used by the next Profile load; there is no Profile update
command.

### Artifact jobs

Artifact jobs collect files for an existing recipe run and make its verified
outputs available for download. Use the run UUID to list jobs and the job UUID
to inspect, resume, submit, cancel, or retrieve one job:

```bash
vonkctl recipe job list --run RUN_UUID
vonkctl recipe job detail JOB_UUID
vonkctl recipe job detail JOB_UUID --follow
```

`recipe job create` takes a bounded JSON binding file. Its top-level keys are
exactly `create` and `input_paths`. The `create` object contains the canonical
artifact-job request fields: `interface`, optional `parameters`, optional
`inputs`, `output_limits`, and `timeout_seconds`. Each input declaration has
exactly `slot`, `name`, and `media_type`. `input_paths` maps every declared
input's safe file name to one local path. The local JSON document is limited to
1 MiB and can declare at most 32 inputs; each local path is limited to 4,096
characters. The Controller's advertised per-file, combined-byte, timeout,
storage, and output limits may be lower. Use values allowed by this run's
compiled recipe contract and the current Controller capabilities.

For example, save a binding beside an `inputs/` directory as
`artifact-job.json`:

```json
{
  "create": {
    "interface": "image-job",
    "parameters": {},
    "inputs": [
      {"slot": "input", "name": "source.png", "media_type": "image/png"}
    ],
    "output_limits": {
      "max_files": 1,
      "max_file_bytes": 1048576,
      "max_total_bytes": 1048576,
      "allowed_media_types": ["image/png"]
    },
    "timeout_seconds": 300
  },
  "input_paths": {
    "source.png": "inputs/source.png"
  }
}
```

Replace the sample interface, slot, parameters, and limits with values accepted
by the run's recipe contract. Relative input paths are resolved from the
binding file's directory; absolute paths are also accepted. Input files must be
readable regular non-symlink files. The CLI computes each file's size and
SHA-256 before creating the draft and rechecks the bytes before upload, so keep
the binding and its input files unchanged when retrying.

```bash
vonkctl recipe job create --run RUN_UUID --file artifact-job.json --request-key REQUEST_UUID
vonkctl recipe job upload JOB_UUID --file artifact-job.json
vonkctl recipe job submit JOB_UUID --request-key SUBMIT_UUID
```

Create reserves a draft using the request key, uploads declared files, and
finalizes the inputs. It does not submit the job. Submit is a separate,
explicit command; only use it after inspecting the ready draft. For scripted
work or recovery after process loss, choose and save a caller UUID with
`--request-key` before creating or submitting. Human create output prints the
generated key before sending the create request, but JSON output is emitted
only when the command returns.

If create's response is uncertain, retry the identical create command with the
same run, binding, input bytes, and request key. The CLI looks up that key and
reconciles the original intent before replaying it. Do not start a replacement
job with a new key while the original create is uncertain. If input delivery
fails after draft creation, retain the draft UUID and binding file, then run
`recipe job upload` for that UUID. It verifies the binding against the draft,
skips complete inputs already accepted by the Controller, uploads missing
files, and finalizes when they are all present. Recovery is at complete-file
granularity; the CLI does not promise byte-offset or range resume for an
interrupted file transfer.

Submit and cancel also accept a caller `--request-key` for stable retries; keep
the same job, key, and intent when retrying. The CLI checks the same job after
an uncertain submit response and verifies both its operation identity and
original submit request key. Reusing that key reconnects to the original
submission; a different key cannot replace it. A job list scoped with `--run`
also refuses results belonging to another run. Cancellation requires `--yes`; `--reason` is optional and defaults to
`Operator requested cancellation with vonkctl`:

```bash
vonkctl recipe job cancel JOB_UUID --yes --reason "No longer needed" --request-key CANCEL_UUID
vonkctl recipe job detail JOB_UUID --follow
```

Cancellation can remain `cancelling` while the worker or agent settles already
issued work. A cancellation receipt is not proof that remote execution has
already stopped; follow the same job until it reaches a settled state. A
bounded follow timeout or Ctrl-C ends local observation only.

Download only works for a succeeded job. The destination must already be an
existing, non-symlink directory. The CLI checks result names and declared
limits, then verifies each file's size and SHA-256 and publishes it atomically.
It reuses an existing file only when that file matches the result manifest;
symlinks, mismatched files, and overwrites are refused. If a collection fails
partway through, already verified files remain and are reported; rerun the
download after resolving the failure to reuse matching files and fetch the
rest.

```bash
mkdir -p artifact-results
vonkctl recipe job download JOB_UUID --output artifact-results
```

## Profile

Profiles cover the entire enrolled fleet. Editing autosaves a permissive draft;
it does not stop or restart workloads. Every Spark remains visible and an
unassigned Spark is explicitly idle. Review resolves the latest verified
cached compatible recipe/model and checks complete topology. Load requires
the digest of that reviewed decision and a client-generated request key.

```bash
vonkctl profile
vonkctl profile list
vonkctl --profile 2 profile name "Coding"
vonkctl --profile 2 profile add "Qwen Code" --spark Atlas --spark Boreal
vonkctl --profile 2 profile add "Qwen Code" --spark Atlas --as coding --state installed
vonkctl --profile 2 profile configure --description "Coding setup" --retention exact --favorite true --label use=code
vonkctl --profile 2 profile remove "Qwen Code" --spark Boreal
vonkctl --profile 2 profile export --output coding.json
vonkctl --profile 3 profile import --file coding.json --expected-revision 0
vonkctl --profile 2 profile load --dry-run
vonkctl --profile 2 profile load
vonkctl --profile 2 profile load --detach
vonkctl --profile 2 profile progress --follow
vonkctl --profile 2 profile progress --application APPLICATION_UUID --follow
vonkctl --profile 2 profile progress --request-key REQUEST_UUID --follow
vonkctl --profile 2 profile cancel APPLICATION_UUID --yes --request-key CANCEL_UUID
vonkctl --profile 2 profile endpoint
vonkctl --profile 2 profile endpoint coding
```

In an interactive terminal, load shows the current review and asks for one
default-no confirmation. `--detach` returns the accepted application instead
of following it. For scripts, JSON, redirected input, or `--no-input`, supply
both the digest you reviewed and `--yes`; `--yes` alone is insufficient:

```bash
vonkctl --profile 2 --json profile load --dry-run > reviewed-plan.json
# Inspect the allowed state and effects, then set REVIEWED_DIGEST to plan_digest.
vonkctl --profile 2 --json profile load --expected-plan "$REVIEWED_DIGEST" --yes --detach
```

A blocked dry-run exits 2 and submits nothing. When the Controller refuses a
load because its reviewed plan changed, the CLI displays one fresh review on
stderr and stops with the original refusal. Review the new effects before
starting another load with its current digest; the CLI never resubmits that
replacement automatically. If the fresh review cannot be read, the original
refusal remains. Do not combine `--dry-run` with `--expected-plan`, `--yes`, or
`--detach`.

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

### Batched recipe qualification

`vonk-fleet-qualify-campaign` executes one reviewed authority batch at a time.
Two single-Spark recipes share one whole-fleet profile and one application;
their smoke suites retain separate recipe, node, run and result identities.
Dual-Spark recipes occupy an exclusive batch. A completed lane never permits
replacing its partner: both results and cleanup must be reconciled before the
next batch. The runner consumes the current generated campaign contract and
rejects retired sequential authorities.
Use the campaign manifest and recipe checkout named by the current qualification
plan, plus a durable ledger outside that checkout. Reserve a dedicated profile
with `installation_policy=keep-cached` and labels
`qualification-authority=<authority ID>` and
`qualification-ledger=<runner ledger identity>`. The ledger identity is the
first 63 hexadecimal characters of the SHA-256 of its resolved absolute path.

The checkout must contain the Git objects for the catalog's exact `source_commit`:
the runner reads its canonical archive validator from that commit, never from
mutable working-tree code. For a shallow checkout, fetch that exact commit from
the reviewed recipe repository before running; the runner never fetches code
implicitly during validation.

Preview requires `--manifest`, `--library-root`, `--ledger`, `--profile-number`,
`--batch`, and the exact Controller `--spark` IDs for that batch. Supply Sparks
in authority assignment order: each single-Spark assignment consumes one ID,
and an exclusive dual assignment consumes two. It saves all batch assignments
in this dedicated profile and records the reviewed preview;
it does not load the profile. Every profile covers the full enrolled fleet,
including Sparks left idle. A dual-Spark row also requires `--failure-spark`
to identify its later recovery checkpoint.

Prepare missing NAS assets through `vonkctl recipe download` before preview.
Spark-local copies may be cold: the accepted plan owns their transfer. If a
healthy workload already occupies the fleet, both preview and apply require
a repeated `--replace-run-id` naming every acknowledged run. Their observed
memberships and planned interruptions become part of the reviewed campaign
digest.

To execute, repeat the reviewed selection with `--apply --campaign-digest DIGEST`.
Add `--accept-operator-gate RECIPE` and `--accept-capacity-review RECIPE` only for
the gates declared by each recipe in the batch. Preview provides the evidence
for these apply acknowledgements. A changed plan or run requires a fresh preview.

Use `--observe` with the same manifest, library, ledger, profile and batch to
reconcile an interrupted submission or advance a recorded physical checkpoint.
Observe takes no Spark selection or acknowledgement flags. After paired smoke,
preview each exclusive recovery transition with `--recover-lane N`, using the
durable lane selection rather than new `--spark` arguments. This preview records
its intent before saving the one-lane profile, and returns a fresh campaign
digest. Repeat with `--apply --recover-lane N --campaign-digest DIGEST` to accept
that exact transition. The preview names the partner workload it will stop and
any exact lane workload it must reactivate. Changed or foreign replacement
effects require a new review; plain observation does not accept a transition.

Follow the reported checkpoint instructions for rank loss and host restart.
Those physical actions are operator-run and happen after other lanes have stopped
and their release is observed. A passing initial smoke is not complete physical
acceptance. The current authority requires dedicated recovery evidence for each
recipe; the runner records canonical coverage receipts bound to the observed
builds, nodes and checkpoints. Preview cleanup with `--cleanup-lane N`, then
accept the exact returned digest with
`--apply --cleanup-lane N --campaign-digest DIGEST`.
The batch advances only after cleanup receipts and fresh Fleet observations
prove release. The explicit cleanup apply records final acceptance or failure
and releases the batch; repeat that same accepted cleanup apply after a lost
response. Observation alone does not finalize or release a batch. Cleanup
retains verified cached assets.

The preview identifies each endpoint/run it will keep or stop, each installation
it will retain or remove, complete distributed Spark groups, and pending orders
it would supersede. It also shows exact model/image identities, which Sparks
can reuse them, the memory pool and system reserve, memory/disk demand and
remaining capacity, required serving and rendezvous ports, and whether reviewed
stops must precede preparation or transfer. Fresh installations undergo the
same port check as existing ones.
A reviewed stop permits reuse only of that exact run's ports. Unknown capacity stays unavailable;
a deficit is shown as “short by.” The planner owns these decisions.

Review does not create or reset build records. Missing cache artifacts remain
blockers for a fresh load. Typed cache-loss recovery of an accepted application
may defer rebuilding to its worker; capacity and invalid-contract blockers still
apply. The plan digest binds semantic effects, eligibility and demand. Changing
free-capacity observations without changing eligibility, transfer counters, or
verification times alone does not change it. A changed memory-reserve policy
does change the review digest, even if the placement still fits. The review
also distinguishes the recipe's memory demand kind from the Spark's physical
memory pool. Host, accelerator and unified reservations compete on a shared
pool; separate host/GPU pools retain independent limits. A changed physical
pool requires a new review. Available memory refers to the current limiting
pool. A conditional memory fit names the exact workloads that must stop before
the Controller checks fresh capacity in every required pool. Reserved peak
memory is not a measurement of what stopping a workload will free. Accepting
this review authorizes those stops; preparation or start can still wait or be
refused if the subsequent capacity check does not pass. Unknown capacity and
demand beyond physical capacity remain blockers before any stop.

An accepted application's intended configuration retains `reviewed_plan_digest`
and its original `reviewed_application_id`, separately from the execution-attempt
`plan_digest`. Retry admission rejects plans that add a workload stop or
installation removal to that original review. The required numbered-load precondition, current-user
checks, and roster/catalog fences are implemented; full resource/deletion
coordination remains tracked in W09 of the implementation plan.

Accepted installation work reserves its reviewed disk space. Other installs
cannot consume that space while the profile is preparing. The reservation
passes to the exact installation and survives a Controller restart. A failed
or superseded profile releases space it has not handed to an installation;
it does not free space still owned by an installation. If another capacity
writer is briefly busy, progress names the dependency and next retry time,
and the same operation continues automatically.

Accepted profiles also hold their required serving and rendezvous ports through
preparation. A workload being replaced keeps its active ports until it stops;
the accepted replacement prevents another workload taking them in between.
Restart reconnects to the original start operation. Fleet's reserved-port count
includes pending profile work and counts an overlapping port once.

After an uncertain submission, the CLI looks up the original request key before
considering a bounded identical replay. A changed profile does not redirect that
lookup to a newer application. An unavailable or denied lookup does not authorize
a new request. Keep the key from the accepted or uncertain-submission receipt and
reconnect with `vonkctl --profile 2 profile progress --request-key REQUEST_UUID --follow`.
When combining `--profile` with `profile progress --application`, the application
must belong to that profile. An application ID alone supports a direct reconnect;
following remains pinned to that exact ID even if a newer application starts.

Edits read the complete saved definition and preserve fields that were not
explicitly changed, including metadata, desired state, and model variant.
`--state` accepts `installed` or `running`; adding Sparks to an existing named
assignment preserves its state and variant unless supplied explicitly.
Loading an `installed` assignment prepares and verifies the installation on
every assigned Spark without starting a runtime or publishing a route. If it
is already running, the review shows the stop; loading stops that runtime and
retains the installation. Progress follows the durable installation work and
waits for every Spark's result. Serving-only memory and port requirements apply
when starting a runtime; disk, preparation, and installation checks still apply
when installing.
Configure can clear the description with `--description ""`, unset a favorite
with `--favorite false`, and remove a label with `--remove-label KEY`.
Conflicting label edits fail before saving. Successful human output says
“Saved; running fleet unchanged.”

Saves include the revision that was read. A conflicting edit is refused; read
the current profile before deciding how to reconcile it. Export writes only
the canonical authoring definition. Without `--output`, stdout is JSON even
without `--json`. A file export creates a new private file and refuses existing
paths or symlinks. Import accepts a regular non-symlink file, or `--file -`
for piped JSON, within the 1 MiB control-document budget. It validates the
definition and requires an explicit `--expected-revision`: use `0` to create
an unused profile number, or the current saved revision to replace it.
Exported files carry no revision precondition; importing never loads a profile.

Cancel a profile application by its exact application ID and explicit Profile
number. Cancellation retains resource claims while already issued effects are
reconciled; it cannot pretend a late start never happened. Use `--detach` to
return the accepted receipt and reconnect with the same application ID. Pending
cancellation appears in Activity with its effect reconciliation. A lost
cancellation response is recovered using the original application and
cancellation request key; retrying does not create another cancellation intent.
This receipt does not mean issued effects have already stopped.

`profile endpoint` shows only routes belonging to the selected Profile's loaded
assignments, with their exact run identity and publication state. An optional
alias narrows the result. An absent, stale, revoked, or unpublished route is not
replaced with a guessed address. Endpoint discovery does not issue credentials
or prove physical model quality.

## Output and recovery

Human results go to stdout; warnings, progress, and errors go to stderr.
Progress is append-only, preserves scrollback, and emits only changes. At narrow widths the renderer
keeps identity, state, and the next action first; redirected output contains no
cursor control. `--wide` requests full columns. `--json` keeps progress and
errors machine-readable, including `operation_id`, `state`, `phase`, byte
counts, warnings, and next actions when supplied by the Controller. JSON emits
one result or structured error on stdout and no progress prose. `--no-input`
suppresses interaction and does not grant consent.

Successful reads exit 0 even when the inspected operation failed. Following
successful work exits 0; an awaited partial result exits 1; failed, blocked,
cancelled or timed-out follows exit 2. A blocked dry-run also exits 2. Ctrl-C
exits 130 and a closed output pipe exits 141; neither cancels Controller work.

Unknown totals are shown as unknown; a build step count is not converted into a
fake percentage. A failed refresh preserves the last verified cache copy.
A new profile load reconciles current fleet/cache state and supersedes older
overlapping requests. Reusing its request key returns the same durable
operation. Observation can be interrupted without cancelling accepted work.
Profile progress without an explicit selector resolves the latest application
once, then pins following to that ID. Reconnect to a particular application
with the exact `--application` or `--request-key` form above.

Observation defaults to 30 seconds (accepted range 0–300), with a one-second
poll interval (0.01–30). These limits do not alter execution deadlines. At
timeout or interruption, JSON contains the intact last response under `result`
and local status under `observation`, including the last observation time,
age, endpoint path, and reconnect command. A local timeout never rewrites the
remote operation state as failed.
