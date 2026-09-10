# vonkctl — CLI experience design

Design proposal · 10 September 2026 · No implementation

This document defines the proposed `vonkctl` experience. Every command and terminal transcript below is a design example, not a claim that the existing `vonkctl` implements it. Names, sizes, rates, progress and hardware readings in transcripts are illustrative.

## 1. The experience

Three things to manage: **Fleet**, **Model**, **Recipe**. One workspace that connects them: **Profile**.

- **Fleet:** my Sparks, their health, and what they are running.
- **Model:** the model files I have and the models I can download.
- **Recipe:** the runnable configurations and images that use those models.
- **Profile:** what I want the entire fleet to run, using the latest cached recipes and compatible models. Edit freely; load when ready. Unassigned Sparks become idle on load.

The ordinary journey is browse, download, assign, load:

```sh
vonkctl model library --family Qwen --usage code
vonkctl recipe library --model "Qwen 3.8"
vonkctl recipe download qwen-code
vonkctl --profile 2 profile name "Coding"
vonkctl --profile 2 profile add qwen-code --spark Atlas
vonkctl --profile 2 profile load
```

`qwen-code` represents a unique human-readable selector returned by the library, not a guessed model or real recipe recommendation. The recipe download also obtains its model if missing. Starting with `model download` is optional.

Bare nouns are useful: `fleet`, `model`, `recipe`, and `profile` each show their overview. No `list` subcommand is necessary. Bare `vonkctl` shows the current profile overview and one next-action hint when appropriate. `vonkctl --help` explains the four entry points in one screen.

## 2. Command map

| Area | Command | Result |
|---|---|---|
| Fleet | `fleet [--watch]` | Fleet table, live models, metrics and client update status |
| | `fleet detail SPARK` | Identity, addresses, hardware, full metrics and workloads |
| | `fleet rename SPARK NEW_NAME` | Change the friendly Spark name |
| | `fleet enroll NAME` | Generate the pairing instructions for a new Spark |
| | `fleet re-enroll SPARK` | Generate certificate replacement instructions |
| | `fleet remove SPARK` | Remove membership and revoke access after reviewing consequences |
| | `fleet upgrade SPARK` or `fleet upgrade --all` | Install the latest eligible signed Spark client |
| | `fleet loginfo SPARK` | Relevant recent agent, monitor and workload logs |
| Model | `model [--watch]` | Downloading, cached and running models |
| | `model library [FILTERS]` | Available models, newest updated first |
| | `model detail MODEL` | Exact variant, usage, sizes, cache and running locations |
| | `model download MODEL` | Download the chosen variant; re-download an existing copy |
| | `model remove MODEL` | Cancel its download and remove its Controller cache copy |
| Recipe | `recipe [--watch]` | Downloading/building, cached and running recipes |
| | `recipe library [FILTERS]` | Library, initially filtered to models present locally |
| | `recipe detail RECIPE` | Model, exposed usage, topology, image and resource requirements |
| | `recipe download RECIPE` | Fetch the image or build it; obtain a missing model too |
| | `recipe update [RECIPE \| --all]` | List available updates, or refresh the specified cached recipes |
| | `recipe remove RECIPE` | Cancel its download/build and remove its cache copy; offer model removal |
| Profile | `profile` | Selected profile, per-Spark assignments and resource totals |
| | `profile list` | Numbered saved profiles and their live match state |
| | `profile name NAME` | Name the selected profile |
| | `profile add RECIPE --spark SPARK ...` | Assign a recipe and immediately save |
| | `profile remove ASSIGNMENT [--spark SPARK ...]` | Remove an assignment or selected members and save |
| | `profile load [--dry-run]` | Apply the entire fleet setup using the latest cached versions; obtain missing assets |
| | `profile progress [--follow]` | Inspect or follow its latest load operation |

Use singular nouns consistently. `fleet rename` describes naming; `profile name` names the selected workspace. This proposed command surface replaces the current hierarchy upon a future implementation; it does not introduce compatibility aliases. No runtime command is changed by this design document.

## 3. Terminal design language

Quiet headings, aligned rows, generous spacing between sections. No full-screen dashboard, nested boxes or decorative banners. The useful information starts immediately below the command.

- Friendly names lead. Stable readable selectors appear where the next command needs them; UUIDs and digests belong in `detail --technical` and JSON.
- Tables use light horizontal dividers, no vertical grid. Numbers align right; names and states align left. Units are always visible.
- Green means Running/Ready, amber means attention, red means failure. Color always repeats a written state. Busy GPU usage alone is neutral.
- Snapshot output is the default. `--watch` refreshes the table in place and keeps its row order stable. Download, upgrade and load commands follow their own progress automatically.
- At 120 columns and wider, show full tables. Below that, use two lines per row; below 80 columns, use compact named blocks. Preserve the identity, state and action first. `--wide` allows deliberate full-width output.
- Honor `NO_COLOR` and terminal capabilities. Non-Unicode terminals use ASCII bars. Redirected output contains plain snapshots or timestamped progress lines, never cursor escapes.
- `--json` emits one structured result to stdout. Progress and human hints never pollute it. With `--watch --json`, emit newline-delimited typed snapshots with documented event semantics.
- Unsupported or stale measurements read `—` with a short reason. Estimates carry `~`. Use GiB/TiB for storage and memory; JSON retains canonical byte values and measurement provenance.

Long names wrap on continuation lines; avoid shortening two distinct variants into the same visible identity. A compact footer explains active filters, catalog age, and missing measurements only when relevant.

### Choosing an object

Every Model and Recipe row shows a copyable `USE` selector, on a continuation line if necessary. Commands accept that selector or an exact unique displayed name. Matching is case-insensitive. Partial matches suggest choices; mutations require an exact selection instead of silently guessing. Multiple matches open a numbered chooser in an interactive terminal; scripts receive an ambiguity error with candidate selectors. Row numbers are never persistent identifiers. Profile rows show the assignment selector too, including any explicit `--as` name.

Spark friendly names should be unique within the Controller. Renaming preserves its identity and profile assignments. Model and recipe selectors distinguish publisher, version and variant as necessary. The CLI offers shell completion from the same catalog facets and enrolled fleet.

## 4. Fleet

```text
Fleet                                      2 online · 1 client update · observed 2s ago

SPARK   STATE   MODELS RUNNING     CPU   GPU   MEMORY GiB   DISK GiB    TEMP   POWER   CLIENT
─────────────────────────────────────────────────────────────────────────────────────────
Atlas   Online  Qwen 3.8           18%   74%     82 / 128   640 / 1920  61°C    89 W  Update
Boreal  Online  Shape model         9%   41%     46 / 128   410 / 1920  54°C    62 W  Current

Qwen 3.8     Atlas    38 tok/s · 0 waiting
Shape model  Boreal   Generating · step 14/30

Upgrade Atlas: vonkctl fleet upgrade Atlas
```

Multiple models use continuation rows under their Spark; its hardware values appear once. A distributed run is labeled `Atlas + Boreal · shared run`; its throughput appears once in the workload summary rather than being summed across ranks. Image/audio/3D workloads show their supported job metrics instead of token rates.

The overview carries the previously defined glance metrics: CPU and GPU utilization, physical memory used/total, disk used/total, GPU temperature, measured power, observation freshness, live model names, supported decode rate and queue pressure. GPU draw is labeled as GPU power in detailed output; a wall or total-system reading must identify its different source. The single GB10 memory pool is counted once.

Client state is `Current`, `Update`, `Upgrading`, `Restarting`, `Failed`, or `Unknown`. Available updates are verified against the Controller's published signed package and the Spark's installed identity. An unavailable update check never becomes `Current`. `detail` shows current and available versions and last checked time.

### Detail and metrics

```text
Atlas                                      Online · observed 2s ago

Technical name  dgx-spark-a1
IP addresses    192.0.2.21 · 2001:db8::21
Client          installed version → available version
Hardware        NVIDIA DGX Spark · GB10 · 128 GiB unified memory
Running         Qwen 3.8 / Qwen Code · profile 2 “Coding”

Memory          82 GiB used · 46 GiB available · observed
Disk            640 GiB used · 1280 GiB free
GPU             74% · 61°C · 89 W · no reported throttling

Rename: vonkctl fleet rename Atlas "Studio Spark"
Logs:   vonkctl fleet loginfo Atlas
```

Addresses above are documentation examples. Actual output distinguishes host name, network interfaces and addresses; technical IDs appear with `--technical`.

`fleet detail Atlas --metrics all` expands the established metrics coverage: GPU clocks/limits/throttling/processes; CPU load/temperature/power when supported; shared memory and pressure; per-device storage and I/O; per-interface network/fabric; runtime identity/readiness; inference throughput, queue/KV/cache, latency and daily summaries; requests/jobs; configured service health and recorded benchmark evidence.

`--range 1h|24h|7d|31d`, `--device`, `--interface`, `--run`, `--capabilities`, and `--json` provide history, selection and export under this one detail entry point. History output retains units, time windows, sampling coverage and support reasons. It never triggers a benchmark. Online with stale telemetry is shown as exactly that.

### Enrollment, removal, upgrades and logs

`fleet enroll Atlas` asks the Controller for a short-lived grant and prints the generated one-time command to run on the Spark. It follows enrollment to connected status, showing any required identity approval. `fleet re-enroll Atlas` follows the equivalent certificate replacement flow while preserving its name and profile references. Credentials never appear in ordinary logs or persistent command arguments. It prints a recovery next step if the host-side command has not been run; waiting forever is unnecessary.

`fleet remove Atlas` explains its current workloads and affected profiles, then confirms removal of membership and revocation. It does not promise physical uninstall or stopping an unreachable host. A running Spark requires explicit workload resolution, including complete distributed groups. Saved assignments remain visible as “Spark removed”; they are not silently erased or reassigned. A genuinely idle removal needs one focused confirmation, not a multi-step ceremony.

`fleet upgrade Atlas` and `fleet upgrade --all` choose the latest eligible signed client known to the Controller, show the target names and any workload interruption, then perform the authorized operation. `--all` proceeds one Spark at a time and pauses on a failed node. Already-current nodes are skipped. Completion requires the intended client version to reconnect with authenticated healthy evidence. An exceptional interruption decision can require a specific prompt; routine upgrade stages do not require repeated approval.

`fleet loginfo Atlas` defaults to relevant recent failures and surrounding context from the client, monitor and active recipes, grouped by source and timestamp. Support `--since 15m`, `--lines 100`, `--recipe SELECTOR`, `--source client|monitor|runtime`, and `--follow`. Bounded, sanitized logs arrive through the authenticated Controller/agent path. An offline Spark shows retained logs with their age. Missing remote collection capability is an implementation requirement, never a reason to fall back silently to SSH.

## 5. Model

`model` lists the union of downloading, cached and running model variants. “Local cache” means Controller/NAS cache throughout this CLI; Spark copies are separately identified in detail.

```text
Models                                     Controller cache · 3 models

MODEL                    USAGE       CACHE / PROGRESS             RUNNING ON   MEMORY~   DISK
────────────────────────────────────────────────────────────────────────────────────────────
Qwen 3.8 · NVFP4         Chat, Code   Cached                       Atlas         72 GiB  64 GiB
  USE qwen-3.8-nvfp4
Vision model · FP8       Vision      Downloading ━━━━━━╸───  68%   —             48 GiB  40 GiB
  USE vision-fp8
Shape model · BF16       3D          Cached                       Boreal        36 GiB  28 GiB
  USE shape-bf16

Vision model  27.2 / 40 GiB · 420 MiB/s · about 31s remaining
Memory is an estimate; exact run requirements depend on the recipe and configuration.
```

Cache state and running location are separate columns because a model can be running while its cache copy is refreshed. An incomplete or failed download remains visible with resumable bytes and failure context. A model running only on a Spark reads `Not cached` in the cache column.

Memory is the declared model estimate/range, or `Recipe-dependent` when no justified model-only estimate exists. Disk is the exact complete artifact size, with downloaded/total separately during transfer. Observed runtime memory lives in Fleet/detail and is not conflated with this estimate. Usage comes from canonical model capabilities; recipe restrictions are shown in Recipe.

### Library

```sh
vonkctl model library
vonkctl model library --usage vision --family Qwen
vonkctl model library --family Qwen --version "Qwen 3.8" --quantization NVFP4
vonkctl model library --usage code --updated-since 30d --sort name
vonkctl model library --filters
```

Columns: model/version/variant, usage, quantization, memory estimate, artifact size, updated date and local cache state. Default order is last updated descending, with name and stable identity as tie-breakers. `--sort name` uses case-insensitive alphabetical order. `--updated-since` accepts a relative duration or ISO date; date boundaries use the displayed client timezone.

```text
Model library · newest updated first

MODEL          USAGE       QUANT.   MEMORY~   DISK     UPDATED      CACHE
Qwen 3.8       Chat, Code   NVFP4     72 GiB   64 GiB   2026-09-10   Cached
  USE qwen-3.8-nvfp4
Vision model   Vision      FP8       48 GiB   40 GiB   2026-09-09   Downloading
  USE vision-fp8

Download: vonkctl model download qwen-3.8-nvfp4
```

`--usage`, `--family`, `--version`, and `--quantization` are repeatable. Values within one filter are OR; different filters are AND. `--search` performs text search. Facets and completion come from existing canonical definitions: do not hardcode a new usage taxonomy or infer support from names. Familiar labels such as Vision, Chat, Code and 3D map to declared values; `--filters` gives the actual supported vocabulary and current matches. No results gives the active filters and one concrete way to broaden them.

Results use bounded pagination with `--limit`, `--cursor`, and `--all`. Interactive paging is optional and never required to select an object. Catalog age is shown; stale or unavailable catalogs retain existing local data and clearly label freshness.

### Download and remove

`model download MODEL` follows an active operation without duplication; resumes an incomplete or failed operation's resolved content and valid partial bytes; and, after completion, re-downloads the latest published revision within the selected variant. It explicitly prints `Following`, `Resuming`, or `Re-downloading`. Another model version, family or quantization remains an explicit choice.

Re-download verifies the replacement before replacing the cached copy; a failure preserves the last verified copy and running workloads. `--detach` returns a short tracking reference. Later `model detail MODEL --watch` follows it.

`model remove MODEL` cancels its active download and removes that variant's Controller cache copies and partial data. One confirmation describes both effects and the reclaimable bytes. Saved profiles, including the active profile, keep their assignments and show `Model not cached`. Running Sparks retain their local copies and continue running. Cache removal never implies stopping workloads or deleting Spark-local files.

If a build or load is waiting for this model, name it and mark it `Model removed; download required`. Dependent work stops or waits at a safe boundary; it must not automatically restart the cancelled download. Record cancellation before cleanup so late workers cannot restore a removed entry. Other cached objects retain shared bytes, excluded from the reclaimed total. Running Spark copies and saved profile references do not block Controller cache deletion.

## 6. Recipe

```text
Recipes                                    Controller cache · 1 update

RECIPE         MODEL                 USAGE       SPARKS   CACHE / PROGRESS       RUNNING ON   UPDATE
──────────────────────────────────────────────────────────────────────────────────────────────────
Qwen Code      Qwen 3.8 · NVFP4       Chat, Code       1   Cached                 Atlas        Available
  USE qwen-code
  Model: ~72 GiB memory · 64 GiB disk    Runtime: ~6 GiB memory · 9 GiB image
Vision Dual    Vision model · FP8     Vision           2   Downloading ━━━━━╸── 68% —            Current
  USE vision-dual
  Model: ~48 GiB memory · 40 GiB disk    Runtime: recipe-dependent · 12 GiB image
Shape Studio   Shape model · BF16     3D               1   Building · step 4/7    —            Current
  USE shape-studio
  Model: ~36 GiB memory · 28 GiB disk    Runtime: ~4 GiB memory · image size pending
```

Resource subrows keep the main table readable. Model memory and runtime overhead state whether values are total or per Spark; per-rank requirements appear in detail. A container image has a disk size, not inherently a RAM size. Therefore the requested image memory is represented as **runtime memory overhead**, if declared or measured, alongside **image disk size**. Unknown overhead is never filled with the image's byte size.

Update state compares the cached revision to the newest compatible published revision of the same recipe. Detail shows both revisions, change date, model/image/resource changes, affected profiles and versions actually running. Failed catalog checks yield `Unknown`. Running old and cached new copies remain individually inspectable; profiles automatically resolve the latest cached version.

### Library

```sh
vonkctl recipe library
vonkctl recipe library --model "Qwen 3.8"
vonkctl recipe library --all-models --updated-since 14d
vonkctl recipe library --sort name
```

The default includes recipes whose exact model variant is downloading, cached or running locally. A partially downloaded model qualifies; a different quantization of the same family does not. The heading explains `For your models · downloading, cached or running` and offers `--all-models`.

An explicit `--model` replaces the local-model default so browsing an uncached model works immediately. Additional filters then intersect with that model selection. `--updated-since`, sorting and pagination follow Model rules. Columns show recipe, exact model/variant, usage exposed by that recipe, Spark count, resource summaries, updated date, and local state. Empty local results point directly to `recipe library --all-models`.

```text
Recipe library · for your models · newest updated first

RECIPE        MODEL              USAGE       SPARKS   UPDATED      CACHE
Qwen Code     Qwen 3.8 · NVFP4    Chat, Code       1   2026-09-10   Cached
  USE qwen-code     Per-Spark runtime ~78 GiB · model disk 64 GiB · image 9 GiB
Vision Dual   Vision model FP8   Vision           2   2026-09-09   Not cached
  USE vision-dual   Per-Spark runtime ~28 GiB · model disk 40 GiB · image 12 GiB

Download: vonkctl recipe download vision-dual
All models: vonkctl recipe library --all-models
```

### Download/build and updates

`recipe download RECIPE` starts with the latest published recipe revision. Its definition chooses image download or build automatically and obtains a missing compatible model. Repeating while active follows the operation; after an incomplete or failed attempt, it resumes that attempt's resolved content and valid completed work. Builds reuse compatible build cache and name the stage being retried. Repeating after completion re-downloads or rebuilds the latest published recipe. A supported builder performs the build, with its identity in detail.

```text
Downloading Vision Dual

Model   Vision model FP8   ━━━━━━━╸──  76%   30.4 / 40 GiB
Image   Vision Dual       ━━━━━━━━━━ 100%   12.0 / 12 GiB · verifying

Model and image will be cached on Controller.
```

`recipe update` by itself lists locally cached recipes with updates. Interpret the unfinished “update all recipes list” requirement as this preview plus `recipe update --all` to update every eligible entry in that displayed set. `recipe update RECIPE` updates one entry. `--all` captures a bounded set at invocation, skips current entries, and summarizes updated/failed/skipped results; one independent failure does not hide the others. Current recipes are not forcibly rebuilt by update; explicit download performs that refresh.

Updates cache new immutable revisions. Every profile immediately shows its latest cached resolution and recalculated resource estimates. Running Sparks keep their loaded copies until `profile load`. Output says `Cached update ready · used on next profile load`. There is no separate profile update command or flag. An in-progress download/build becomes the latest usable cache copy only after verification completes.

### Removal

`recipe remove RECIPE` cancels the recipe's active download/build and removes its Controller-cached revisions and partial data, reporting actual reclaimable bytes after shared-layer accounting. All profiles keep their assignments and show `Recipe not cached`, including the active profile. Running Sparks continue on their own copies. Dependent loads are marked `Recipe removed; download required` and cannot recreate it until a new explicit download/load action. Cancellation prevents a late build result from republishing the removed entry.

If this is the last locally cached recipe for the exact model variant, include one combined confirmation:

```text
Remove Shape Studio?                      4.2 GiB reclaimable
This is the last cached recipe for Shape model BF16.
Also remove its unused model files?       28.0 GiB reclaimable

[1] Remove recipe, keep model  (default)
[2] Remove recipe and model
[3] Cancel
```

Saved or active profiles do not prevent this optional model-cache removal. Choosing it also cancels the model download and applies the dependent-operation handling described above. Recipe-only removal leaves the model and any shared or separately requested model download intact, and states that outcome. Scripts select `--with-model` or `--keep-model`; `--yes` alone keeps the model. Shared bytes retained by other cached objects are excluded from reclaimed totals. Spark-local copies and profile references are independent of Controller cache retention.

## 7. Profile: the selected workspace

Every invocation has a selected profile. Without `--profile`, it is profile **1**, initially named **Default**. `--profile 2` selects profile 2 for that invocation only. Numbers are stable within a Controller and shared across clients; they are never row positions or silently renumbered after removal. The CLI never retains a hidden “last selected” profile. Show number and name at the top of profile output.

```sh
vonkctl profile
vonkctl --profile 2 profile
vonkctl --profile 2 profile name "3D development"
```

A read of an unused number shows `Profile 2 · not yet created`; the first explicit edit creates and saves it. Names are editable labels; numbered identity remains stable. Selected, saved and actually loaded state are distinct: selecting profile 2 does not change running workloads.

Profiles save recipe choices and Spark assignments, not revision pins. Resolve the latest verified cached recipe and latest cached model compatible with its declared contract and selected variant. Latest follows published revision ordering, not cache insertion time. Never substitute another family or quantization. A recipe binding an exact model revision still requires that revision; “latest” cannot override compatibility.

When Controller assets are removed, the overview shows `Model not cached` / `Recipe not cached` alongside actual Spark status, even for the active profile. Reading never downloads anything. Loading reuses compatible verified Spark copies and obtains missing assets for targets that need them. If no cached recipe exists, resolve its current library definition and show the required download/build. A cached recipe does not upgrade merely because a newer upstream version exists.

Each load freezes its resolved content and fleet snapshot for that execution. Subsequent cache updates affect the next load, not the running operation. These execution records support verification without adding version management to profile editing.

### Overview

```text
Profile 2 · 3D development                 Saved · changes not loaded

SPARK    RECIPE / MODEL                         CONTROLLER CACHE       ON SPARK
───────────────────────────────────────────────────────────────────────────────────
Atlas    Shape Studio / Shape model BF16         Cached                 Different run
         USE shape-studio
         Vision Dual / Vision model FP8         Model not cached       Not running
         USE vision-dual
         Memory after load   ~68 / 128 GiB · ~60 GiB headroom
         Additional disk      52 GiB needed · 180 GiB available
         Combined model/image footprint: 89 GiB

Boreal   Vision Dual / Vision model FP8          Model not cached       Copy available
         USE vision-dual
         Memory after load   ~28 / 128 GiB · ~100 GiB headroom
         Additional disk       0 GiB needed · 320 GiB available
         Combined model/image footprint: 52 GiB

Vision Dual: Atlas + Boreal · 2/2 Sparks assigned
Controller cache: Vision model missing · Boreal retains a verified local copy
Fleet footprint: 89 GiB unique artifacts · 141 GiB across Spark disks

Load this setup: vonkctl --profile 2 profile load
```

Examples assume declared per-rank requirements, not model size divided by Spark count. `Memory after load` includes concurrent runtime memory, context/KV requirements and system/non-profile allowance; illustrative totals assume those allowances are included. Headroom uses that complete estimate. Detail separates each component and observed current use. Missing estimates produce a known subtotal plus `unknown`, never a reassuring full total.

`Additional disk` is incremental required space, including peak temporary/import needs and reuse of verified local content; compare it directly with observed available space. Combined footprint counts unique model/image bytes once per Spark. Controller missingness and Spark availability are separate facts. Removing an assignment changes desired resource use without necessarily freeing physical disk.

The narrow-terminal layout preserves the same decisions:

```text
Atlas · profile 2 “3D development”
  shape-studio   Cached             Different run
  vision-dual    Model not cached   Not running
  Memory after load  ~68/128 GiB · ~60 GiB headroom
  Additional disk     52 GiB · 180 GiB available
```

An active profile after Controller cache deletion can show:

```text
Profile 2 · 3D development · Active
Atlas   shape-studio   Recipe + model not cached   Running
Running on Atlas's local copy. Controller cache was removed.
```

### Add/remove and autosave

```sh
vonkctl --profile 2 profile add shape-studio --spark Atlas
vonkctl --profile 2 profile add vision-dual --spark Atlas
vonkctl --profile 2 profile add vision-dual --spark Boreal
vonkctl --profile 2 profile remove vision-dual --spark Atlas
```

`profile add` saves a library/local recipe choice and its Spark assignments. Feedback resolves latest cached compatible versions without pinning the profile. Missing Controller assets are labeled explicitly; per-Spark feedback explains whether a verified copy is reusable or downloads are needed on load. Repeat `--spark` to assign a complete group in one edit. Repeating the same assignment to the same Spark is idempotent.

For a distributed recipe, a later add to another Spark extends its one unambiguous existing assignment. To create independent copies or groups, use an explicit `--as NAME`, for example `--as vision-east`; this name becomes the assignment selector. If multiple assignments could match, prompt for the assignment instead of merging groups arbitrarily. Rank mapping comes from the Controller and is available in detail.

Every edit is a durable Controller save, with revision/concurrency checking. Print `Saved` only after acknowledgement. A conflicting concurrent edit reports the conflict and preserves the user's intent for retry; it never silently overwrites another client's work. There is no Save command and no requirement to complete a profile before saving.

```text
Saved profile 2 · 3D development
Added Vision Dual to Atlas.

Atlas memory after load  ~40 → ~68 GiB / 128 GiB · ~60 GiB headroom
Additional disk            0 → 52 GiB needed · 180 GiB available
Combined recipe disk      37 → 89 GiB
Info: Vision Dual needs 2 Sparks; 1 is assigned. Add one more before loading.
```

Adding the third Spark to a two-Spark assignment still saves successfully:

```text
Saved. Info: Vision Dual needs 2 Sparks; 3 are assigned.
Choose the two members before loading this assignment.
```

Memory pressure, insufficient storage, offline targets and incomplete groups are informational while editing. Give immediate per-Spark before/after totals for each add/remove, including shared-artifact reuse. Unknown resources explain what remains unknown.

### The entire fleet, every time

Every profile covers every currently enrolled Spark. The overview shows every Spark with its recipes or explicitly `Idle`. There are no include/exclude controls. Removing a Spark's last assignment says `Atlas will stop its managed recipes on the next load`. Editing alone never stops workloads.

New profiles initially show every Spark as Idle. Newly enrolled Sparks automatically appear as Idle in every profile without immediate runtime changes. Loading an empty profile idles the entire fleet and states that outcome prominently. Removed Sparks leave the roster; unresolved assignments remain named for correction, especially when a distributed group becomes incomplete.

Resolve and validate the complete current fleet on load. Offline Sparks are named as preflight blockers before workload replacement. A membership change during a load is reported; a fresh whole-fleet load reconciles it. Do not claim complete success for nodes outside the execution's frozen fleet snapshot.

### Loading

`profile load` means “make the entire fleet run this setup using the latest cached compatible recipes and models.” Reuse verified Spark copies, obtain missing assets through Controller as needed, deliver/verify them, stop managed workloads absent from this profile, start its recipes and check readiness. Unassigned Sparks become idle. Cached updates take effect automatically on load; downloading updates never restarts live workloads. There is no profile update command, update flag or mandatory manual preparation.

An invalid group is an informational save condition but a load blocker. Preflight the complete profile before changing any running workload. A two-Spark recipe assigned to one or three nodes cannot be launched truthfully; loading reports `Profile saved; not loaded` and the exact corrective command. It neither guesses a group nor silently loads just the valid subset.

`--dry-run` shows current → desired changes for every Spark, resolved versions, missing assets and resource fit without loading. Ordinary load binds the Controller plan internally. Submitted content remains fixed for that execution; cache updates cannot swap bytes mid-load. Changed topology or incompatible model requirements must be resolved before execution rather than guessed.

```text
Loading profile 2 · 3D development
Atlas: Qwen Code → Shape Studio + Vision Dual
Boreal: Idle → Vision Dual

Controller   Model files       ━━━━━━━━━━ 100%  Verified
Controller   Recipe images     ━━━━━━━━━━ 100%  Verified
Atlas        Copying assets    ━━━━━━━╸──  76%  39.5 / 52 GiB
Boreal       Copying assets    ━━━━━╸────  54%  28.1 / 52 GiB

Next: start Shape Studio on Atlas; start Vision Dual on Atlas + Boreal.
```

Stage before stopping existing workloads where resource and runtime constraints permit. Preserve prior assets for reuse and recovery. Show unavoidable interruption and any recipe-specific preparation phase. Do not promise atomic whole-fleet replacement or automatic rollback after runtime failures.

Completion shows each assignment as `Running` for services or `Ready for jobs` for artifact workers, with Spark names and endpoint/usage details where available. A distributed assignment requires all ranks and coordinator readiness. A failed rank produces a failed/degraded group, never partial “Running” success.

`profile progress` shows the latest durable load; `--follow` attaches. Interrupting observation leaves accepted work running. Repeating load while it is applying follows that frozen execution. After failure, a new load resolves current fleet/cache state, reports any newly selected cached versions and reuses compatible completed work. A successful entire-fleet load identifies the active profile. A partial switch names actual per-Spark state and the attempted profile without claiming full activation. Removing Controller cache does not make a healthy running profile inactive. New cache versions show `New cached version · next load` beside the actual running copy.

## 8. Progress and recovery

Use slim horizontal line bars with numerical progress, not decorative trend sparklines. Download percentage is received bytes divided by known total bytes; integrity verification is a separate phase. Display transferred/total, current rate and ETA only when estimable. Transfer 100% can still say `Verifying`; `Cached` requires successful verification and publication.

Builds have a meter too. When a builder exposes genuine bounded work, show its reported percentage with source. Otherwise show an indeterminate moving segment plus stage count and current step:

```text
Building Shape Studio   ───╺━━╸────   Step 4/7 · compiling runtime · 2m 14s
```

Do not convert step 4/7 to “57% done”: build steps can differ enormously in duration. A builder with no step total shows the current phase and elapsed time. Completion becomes 100% only when the final image is verified. Multiple model/image transfers appear as separate lines; any overall byte percentage is labeled transfer progress, not total load completion.

Failure output leads with object, phase, preserved work and next command:

```text
Vision Dual could not finish copying to Boreal: connection lost.
Atlas is staged. Boreal has 28.1 / 52 GiB. Existing runs are unchanged.
Retry: vonkctl --profile 2 profile load
Logs:  vonkctl fleet loginfo Boreal --recipe vision-dual
```

Those preservation claims must reflect the actual operation: if stopping already occurred, name what is still running and what stopped. Interrupted transfers resume when identity and server support permit. Unknown submission results reconcile through the same durable request identity. Detached operations remain discoverable from the relevant model, recipe or profile; a new top-level Jobs concept is unnecessary.

## 9. Predictable automation

`--json`, `--no-input`, `--detach`, `--timeout`, and `--dry-run` are consistent wherever applicable. `--yes` answers a described destructive confirmation but never chooses an ambiguous object, changes topology or broadens scope. Routine download, assign and load commands express sufficient intent without an extra `--apply`.

Noninteractive ambiguity or a required consequential decision returns a typed actionable error. In JSON, include canonical identities and units, selected profile, desired/observed states, operation reference, request identity, freshness, warnings, and next actions as applicable. Use authoritative typed contracts; avoid another handwritten wire schema.

Proposed exit semantics: `0` completed read/edit/verified operation; `2` invalid input or required selection; `3` blocked or failed operation; `4` partially applied operation; `5` still running after an observation timeout. Explicit `--detach` exits `0` after acknowledged submission with `state: accepted`, which is clearly distinct from execution success. Ctrl-C exits `130` and reports that accepted work continues. Authentication/transport errors retain a stable typed cause and cannot be reported as an empty fleet or library.

The CLI connects to the Controller using the existing supported secure connection and credential mechanism. First-run help explains the Controller origin and credential setup in one short recipe. No fifth top-level configuration concept is required for this design.

## 10. Acceptance for the eventual implementation

1. A new user can find a model, obtain its recipe, assign it to named Sparks and load the profile using only help and the next-action hints.
2. Every requested overview has readable wide and narrow output; redirected output is plain, and JSON is parseable without scraping terminal prose.
3. Fleet shows all agreed glance metrics and exposes the full existing coverage through detail. Missing or stale data, update-check failures and distributed metrics are truthful.
4. Local Model and Recipe views include active preparation, cached and running states concurrently. Recipe library defaults to exact local model variants and explains how to broaden it.
5. Both progress meters handle unknown totals without invented percentages. Failed refreshes preserve existing verified content.
6. Active download commands attach, failed/incomplete attempts resume valid work, and completed downloads refresh on repetition. Update-all refreshes only outdated recipes. Profiles automatically resolve latest cached compatible versions; running workloads change only on load.
7. One-, two- and three-member drafts of a two-Spark recipe save with appropriate information. Only valid complete groups load, and no load mutation starts when preflight blocks the profile.
8. Edits autosave with concurrent-write protection, show resource deltas, and never change live workloads. Profile selection defaults deterministically to 1.
9. Resource totals distinguish shared physical memory, runtime overhead, unique disk content, retained cache and incremental transfer space. Unknown totals remain explicit.
10. Profile load covers the entire fleet, idles unassigned Sparks, reuses verified copies, obtains missing assets and reports observed readiness and partial failures. Newly enrolled Sparks appear as Idle; membership changes are detected.
11. Removing a model/recipe cancels its download/build and removes Controller cache without changing profile assignments or running Spark copies. Active profiles show missing cache too. Dependent operations cannot recreate removed content without a new explicit action. Last-recipe removal offers model deletion and preserves shared bytes owned by other cached objects.
12. Upgrade uses signed Controller operations and named targets. Log retrieval stays on the authenticated Controller path. Enrollment/re-enrollment makes the necessary host action understandable.

## 11. Design handoff

This proposal adopts the user's Fleet / Model / Recipe structure and Profile loading workflow. It preserves the documented automatic Controller preparation behavior and the established metrics coverage. Prior Fleet/Library navigation and command-tree proposals are background, not constraints on this new CLI design.

Before implementation, map each proposed behavior to the current canonical API: particularly friendly selectors/profile numbering, log retrieval, cancellation and cache eviction independent of running Spark copies, automatic latest-compatible cache resolution, whole-fleet profiles, distributed draft editing, autosave concurrency and resource projections. Any absent behavior is an implementation gap; none is advertised as already shipped here.

The CLI's acceptance comes first. The future web experience can use these proven tasks and contracts once they work end to end. This document changes no Python/Rust/TypeScript code, recipe definitions, published commands, deployment or live Spark state.

This revision incorporates the user's explicit review decisions: whole-fleet profiles, latest cached compatible content, removal as cancellation, and independent Controller/Spark cache lifetimes. Those decisions supersede older profile-pinning or retention assumptions in the background documents.

Design evidence: [current CLI guide](/opt/vonk-forge/docs/runbooks/vonkctl.md), [metrics contract](/opt/vonk-forge/docs/interface-metrics-spec-2026-09-04.md), [automatic Controller preparation](/opt/vonk-forge/docs/controller-preparation-contract-2026-09-05.md), [existing interface requirements](/opt/vonk-forge/docs/interface-implementation-spec-2026-09-04.md), and the canonical Model/Recipe package in `/opt/vonk-forge-recipes/contracts`.
