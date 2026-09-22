# CLI implementation status

Implementation is active on `codex/cli-operator-experience`, based on
`49389f3dced50d2ecf7680e256dbe1827817eaee`. The full objective remains the
[twenty-package implementation plan](cli-operator-implementation.md).
This record distinguishes implemented pieces from completed packages; nothing
below claims deployment, hardware acceptance, or complete web parity.

The user authorized implementation and routine decisions, including necessary
fresh-schema changes, and confirmed there is no active production environment.
Existing unrelated `.tmp/` files and `docs/handover-spark-canary.md` are preserved.

## Package state

| Package | State | Current evidence / remaining work |
| --- | --- | --- |
| W00 | Inventory established | Current command/route map below; initial 75 focused tests passed in 7.74s. Node-profile owner identified; its missing command belongs to W05. |
| W01 | Implemented process foundation; package open | Contextual exits, human streams, JSON, explicit profile selection, finite timing, no-input, Ctrl-C and pipe handling. Remaining: apply the consent policy to newly delivered consequential workflows. |
| W02 | Started | Human control characters escaped; progress append-only; warnings on stderr. Explicit canonical renderers, widths and empty states remain. |
| W03 | Implemented; final qualification pending | Offline orientation/help/version, parser-derived Bash/Zsh completion, connection check, nonblocking rejection of nonregular credential files. Operator walkthrough remains W19. |
| W04 | Planned | Private enrollment-grant delivery and uncertain issuance recovery. |
| W05 | Planned | Projection/readiness work, complete bounded selection, node-profile surface, and presentation. |
| W06 | Implemented observation foundation; package open | Exact profile ID pinning; profile request/application selectors; noun-owned model/recipe/fleet-job progress; bounded sleep/request budget; distinct timeout/interruption documents and reconnect commands. Receipt/recovery integration for later workflows remains. |
| W07 | Planned | Canonical authoring definition read and lossless edits/import/export. |
| W08 | Planned | Cache request lookup and durable ambiguous submission recovery. |
| W09 | Planned | Exact reviewed decision, replay binding, atomic admission and real races. |
| W10 | Planned | Review/confirmation and scripted exact-plan loading. |
| W11 | Planned | Model cancellation separate from eviction. |
| W12 | Planned | Recipe cancellation with shared-child ownership. |
| W13 | Planned | Profile cancellation and issued-effect reconciliation. |
| W14 | Planned | Canonical activity pagination and authorized resume. |
| W15 | Planned | Profile-to-published-endpoint discovery. |
| W16 | Planned | Complete artifact-job input, execution, and verified-output flow. |
| W17 | Planned | Reviewed removal effects and sequential signed fleet maintenance. |
| W18 | Started | Real entry-point and shell tests; wheel build and existing signed updater installation verified. Final installed surface and all implementation checks remain. |
| W19 | Planned | Actual service/PostgreSQL/storage acceptance and operator walkthrough; no deployed or physical claim. |

## Current surface and owners

Reads use authenticated Controller routes; mutations use the owning route's
`auth.MUTATION_ROLES` policy. Local flags never supply authority. Request and
response structures are validated from the generated current OpenAPI.

| CLI operation | Current transport / owner | Identity, side effect and observation |
| --- | --- | --- |
| `fleet`, `fleet detail` | `GET /api/fleet`, `/api/fleet/{selector}`; FleetProjection | Read exact node/roster projection; filtered bounded watch |
| `fleet rename` | `POST /api/fleet/{selector}/rename` | Exact enrolled node; changes friendly name |
| `fleet enroll`, `re-enroll` | `POST /api/fleet/enroll`, `/api/fleet/{selector}/re-enroll` | Enrollment authority issues credential; private handoff pending W04 |
| `fleet remove` | `POST /api/fleet/{selector}/remove` | Node revocation/removal; final consent treatment W17 |
| `fleet upgrade` | `POST /api/fleet/upgrade`; AgentUpgradeService | Signed upgrade; receipt's current `operation_id` identifies a job; W17 will complete following and remove all-at-once |
| `fleet progress JOB_ID` | `GET /api/jobs/{job_id}` | Exact job snapshot; `--follow` awaits its outcome |
| `fleet loginfo` | `GET /api/fleet/{selector}/loginfo` | Bounded collected diagnostic read; no SSH |
| `model`, `recipe` | `GET /api/model`, `/api/recipe` | Controller cache/operation views; bounded watch |
| `model library`, `recipe library` | Singular noun `/library`; LibraryProjection | Page/cursor and task facets; public catalog differs from cached availability |
| `model detail`, `recipe detail` | Singular noun `/{selector}` | Exact selector/detail; technical option and bounded watch |
| `model download`, `recipe download` | Singular noun `/{selector}/download` | Schema-2 request key; follows noun operation or detaches |
| `recipe update` | `POST /api/recipe/update` | Lists or requests updates; owner-specific receipt |
| `model remove`, `recipe remove` | Singular noun `/{selector}/remove` | Explicit cache eviction; recipe dependency choice; not cancel-only |
| `model progress ID`, `recipe progress ID` | Singular noun `/operations/{operation_id}` | Read succeeds independently of remote state; follow awaits terminal result |
| `profile`, `profile list` | `GET /api/profile/{number}`, `/api/profile` | Stable numbered profiles; reads may default visibly to 1 |
| `profile name`, `add`, `remove` | `GET`, then `PUT /api/profile/{number}` | Explicit profile selection and revision; current lossy projection must be replaced in W07 |
| `profile load --dry-run` | `POST /api/profile/{number}/preview` | Read-only review; blocked preview exits 2 |
| `profile load` | `POST /api/profile/{number}/load` | Durable request/application; required reviewed-plan precondition pending W09/W10 |
| `profile progress` | Numbered latest/request lookup, then `/api/profile/applications/{id}` | Resolves latest once; follows exact identity; direct application checks profile membership |
| `--check-connection` | Local token/origin checks, then `GET /api/fleet` | Read only; no configuration or credential issuance |
| No command, help/version, `completion` | Local parser/build metadata | No credentials, remote lookup, or update check |
| `update`, `update --apply` | Signed installer publication; CLI updater | Checks or installs the CLI wheel, independently of Controller/fleet upgrade |

The web's profile save/load, library/cache, fleet, activity/resume, and
artifact-job methods were checked in `control/web/src/api/client.ts`. Activity,
artifact jobs, endpoint discovery, and complete authoring remain assigned to
their packages above. Browser authentication/token issuance remains the
explicit bootstrap dependency; it is not silently labelled CLI parity.

`AgentNodeProfile` and `FleetProjection._node_profiles` own the node identity
and lifecycle projection. They do not represent a whole-fleet workload profile.
The guide's current `fleet node-profile` command is missing from this parser;
W05 must expose that node-owned capability through its canonical projection,
without restoring `fleet profile` or using workload-profile endpoints.

## Verification log

- Before edits: 75 existing command/transport/error tests passed in 7.74s.
- New process regressions reproduced human errors on stdout, failed-read exit
  confusion, successful blocked previews, online no-command startup, silently
  clamped/NaN wait values, implicit profile mutation, progress in result pipes,
  repeated broken-pipe writes, terminal-control injection, and missing offline
  completion before their fixes.
- Exact-follow regression reproduced switching to a newer application;
  deadline regression reproduced a 30-second sleep under a 0.1-second budget.
- Fleet-filter regression reproduced dropping filters after the first read.
  Credential FIFO regression reproduced a blocking open before file validation.
- Focused CLI/process/client/error/presentation/update run: 119 passed in
  13.01s at the observation-foundation checkpoint, including the real updater
  installation and shell syntax/completion checks.
- Existing connected CLI contract harness: 1 passed. It remains a small test
  server and is not evidence for real admission or worker behavior.
- Existing signed updater suite, including real wheel installation in a fresh
  environment: 12 passed in 7.52s with a task-specific dependency cache.
- Root Ruff lint passed. Python types passed with the existing one reviewed
  exception; no new allowance. Web dependency install and production build passed.
- Formatting checks pass for all 11 changed/new Python files at this
  checkpoint. Repository-wide formatting also reports unrelated pre-existing
  files, including `.tmp/recipes-reasons.py`; those files were not reformatted.
- Rust wire regeneration check and supply-chain verification passed. The
  curated file-digest manifest was regenerated with the changed build inputs.
- No actual profile admission race, process/storage cancellation acceptance,
  deployed Controller, physical Spark, or operator usability gate has run yet.

Next: finish checking and recording the process/observation foundation, then
deliver canonical profile authoring and decision-oriented inspection. Keep the
full plan active; these foundations do not complete the CLI objective.
