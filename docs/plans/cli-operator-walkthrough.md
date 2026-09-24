# W19 operator walkthrough protocol

**Status:** no human participant acceptance has been performed. The eight
human tasks (U1–U8) and the thirteen connected W19 fault scenarios are separate
acceptance tracks. A card can supply usability evidence for its mapped scenario,
but it does not replace that scenario's connected evidence; a passing automated
scenario does not fill a human card. This protocol does not close W19.

The source criteria are the [W19 implementation plan](cli-operator-implementation.md#operator-walkthrough-and-acceptance-scorecard).
The participant receives a copy of the shipped [`vonkctl` runbook](../runbooks/vonkctl.md),
the installed executable, connection details, and the outcome-only task cards
below. The participant must not be shown facilitator setup, expected outcomes,
tests, source, or a command sequence. They may use the shipped runbook; record
each lookup and any request for help. Before starting, record the participant's
role and prior familiarity with Vonk, `vonkctl`, the terminal, and this
repository. After each task, ask the participant to identify unclear terms and
their interpretation without explaining them.

This is a facilitator-mediated discovery exercise, not a technically isolated
test: the fixture does not sandbox the host filesystem or network. Record any
source/test access, outside research, or implementation hint as assisted
evidence. Do not call it unassisted if any occurred. A plan or intention to run
every model is not evidence that a participant completed any task or model run.

## Outcome-only task cards

Give the participant these tasks verbatim, one at a time. Do not demonstrate a
command or tell them which runbook section to use.

| ID | Task to give the participant |
| --- | --- |
| U1 | Find help while offline. Identify a seeded invalid Controller connection or expired-token case, then complete a named Fleet read with authorized access without exposing the credential. |
| U2 | Find a recipe that fits the Fleet but is not ready. Explain its exact blocker and next action. Also find a usable candidate beyond the first page. |
| U3 | Create and edit a Profile with an installed-only assignment and metadata. Export and import it, then explain why saving has not changed running workloads. |
| U4 | Review a whole-Fleet load, identify affected and idle Sparks, and explain its effects before accepting. Handle a stale review without executing changed effects, then repeat the consent decision noninteractively. |
| U5 | Interrupt observation of accepted work, open a fresh shell, and reconnect to the same application. Explain how the observation result differs from the remote operation result. |
| U6 | Explain a durable blocker and use only its advertised authorized action. Cancel work after some effects have been issued; explain which effects completed, remain pending, or were never issued, and whether verified assets remain usable. |
| U7 | Find a current endpoint owned by the loaded Profile. Separately retrieve verified artifact-job files, and distinguish unavailable output from an empty successful result. |
| U8 | Explain the scope of a cleanup action and the one-at-a-time upgrade rule. Use JSON in a pipe for a safe task and show that output, exit status, and consent behavior are predictable. |

The cards test discovery, not memorization of command names. A correct result
that requires an implementation hint, guessing a command, or manually
inspecting JSON is recorded as a usability gap. Do not let a participant
perform cleanup or upgrades on production resources. After each task, ask,
“Which words or phrases were unclear or had more than one plausible meaning?
What did you understand each to mean?” Record the participant's exact term and
interpretation before any explanation; this is a neutral measurement prompt,
not an implementation hint.

## Facilitator setup — do not give to participants

The opt-in [`test_cli_operator_walkthrough.py`](../../control/tests/test_cli_operator_walkthrough.py)
provides a disposable first-connection, recipe-discovery, and Profile-authoring
setup. Its smoke mode exercises an independently built CLI wheel, the
registered HTTPS API, and PostgreSQL-backed catalog, Fleet, cache-assessment,
recipe-preparation, and Profile owners. The invalid-origin environment file and
valid-but-expired token file are seeded by the fixture; its smoke proves a
connection-boundary failure and a successful authorized Fleet read. A nonzero
exit from an unrecognized CLI option is not connection coverage.

The interactive fixture copies the shipped runbook to
`<temporary workspace>/participant-materials/vonkctl.md`, outside the operator
shell directory, and prints its absolute path with the label `Participant
runbook copy:`. It also prints `Valid connection case:`, `Seeded invalid
connection case:`, `Seeded expired token case:`, and `Valid token file:` with
their respective paths. Hand the participant that runbook copy and the
connection materials. If any of these five labels or paths is absent, do not
start the session. Before closing this U1–U3 fixture, retain the printed
runbook outside its temporary workspace for the later sessions; normal fixture
teardown removes that workspace. For example, copy the exact printed path to a
facilitator-controlled location, replacing the source placeholder with the
path printed by the launcher:

```bash
mkdir -p "$HOME/Documents/vonk-w19-participant-materials"
cp "<printed Participant runbook copy path>" \
  "$HOME/Documents/vonk-w19-participant-materials/vonkctl.md"
```

The U4–U8 launchers do not create another copy. Use the retained runbook with
each later card, and do not point participants at a path inside a disposable
fixture or at this repository checkout.

Without an asset override, the smoke uses a synthetic missing-asset row and
verifies that its advertised action creates a PostgreSQL-backed queued
preparation request. The cache-ready synthetic entry has an empty image and
does not establish a runnable candidate. With `VONK_QWEN3_CPU_ASSET_ROOT` set
for interactive mode, the setup verifies a pinned Qwen3-0.6B BF16 snapshot and
locally derived CPU runtime archive. The Qwen blocked row identifies that exact
model and its missing image. The separate Qwen CPU test asserts that the
advertised action queues a durable preparation operation for that exact
Qwen-image blocker, then verifies real local inference with the prepared
model/image. It starts no preparation worker. This Qwen assertion and the
default synthetic queued-request assertion are separate evidence. Model bytes
are served only by a request-restricted local `httpx.MockTransport`; Skopeo
inspects the pinned runtime archive before its digest is published through
`RecipeImageAvailabilityService` and managed storage. The fixture starts no
background worker or Spark workload. The queued-request checks and local CPU
inference establish neither model quality nor physical Spark acceptance.
Missing Skopeo produces a skip, which the required-execution plugin turns into
a failed acceptance command. U4–U8 operational
scenarios remain unavailable in this launcher. Separate [U4 whole-Fleet review](cli-u4-whole-fleet-facilitator.md),
[U5 observer setup](cli-observer-walkthrough.md), [U6 cancellation setup](cli-cancellation-walkthrough.md),
and [U7 results setup](cli-results-walkthrough.md) exercise stale-review consent,
exact-ID reconnect, cancellation across worker restart, and result retrieval. The [U8 read-only pipeline](cli-operator-u8-readonly.md)
checks actual process composition. The [U8 upgrade setup](cli-upgrade-walkthrough.md)
provides a separate disposable shell with two test nodes and a controlled first-target
failure through the real job owner. The [U8 cleanup setup](cli-cleanup-walkthrough.md)
adds a disposable review, consent and removal scenario; its integrated smoke
qualification is recorded in the [current W19 package row](cli-operator-status.md#package-state).
W19 stays open and
starting any shell does not imply a participant result.

The separate opt-in
[`test_cli_linked_profile_journey.py`](../../control/tests/test_cli_linked_profile_journey.py)
connects Profile authoring and import to a reviewed application, exact-ID
progress lookup, and that same Profile's published endpoint. Its smoke mode
accepts a fresh reviewed running revision and follows the resulting application
ID from a new installed-CLI process. Its interactive mode stops after importing
the Profile in installed-only state and leaves the edit, review, consent,
progress, and endpoint discovery to the operator. A bounded local facilitator
process advances only issued owner work and posts a genuine grant-bound run
observation through the registered API. Its typed local agent receipts exercise
the Controller owner chain; they do not launch a runtime, start a model, or
contact a Spark. The published route is consequently an ownership and identity
projection, not a usable inference endpoint. This covers the connected
Profile-to-endpoint process seam; it does not complete U2's runnable-candidate
prerequisite, artifact-result retrieval in U7, or the U1–U8 human scorecard.

Launch the linked owner-boundary smoke with:

```bash
VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes \
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
VONK_LINKED_PROFILE_JOURNEY_MODE=smoke \
PYTHONPATH="$PWD:$PWD/src:$PWD/control/src" \
uv run --project control --frozen --with-editable . \
  pytest -p control.tests.required_execution -q -s \
  control/tests/test_cli_linked_profile_journey.py::test_installed_cli_links_profile_import_load_progress_and_published_endpoint
```

The companion human selector is
`control/tests/test_cli_linked_profile_journey.py::test_installed_cli_linked_profile_interactive_session`;
set `VONK_LINKED_PROFILE_JOURNEY_MODE=interactive` and select that node together.
That optional end-to-end session begins with the imported Profile installed-only;
the facilitator process is bounded by shell lifetime, rate-limits its polling,
and refreshes the fixture node's inventory through the registered agent route
once per minute. Its clock follows elapsed time while the operator reads instead
of fast-forwarding the age of saved inventory. Record only the U1–U8 outcomes
actually completed; this extra session does not replace a card. Use only the
printed loopback Controller and disposable credential.

From the repository root, launch the noninteractive connection/discovery/authoring smoke with:

```bash
VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes \
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
VONK_WALKTHROUGH_MODE=smoke \
PYTHONPATH="$PWD:$PWD/src:$PWD/control/src" \
uv run --project control --frozen --with-editable . \
  pytest -p control.tests.required_execution -q -s \
  control/tests/test_cli_operator_walkthrough.py::test_disposable_cli_operator_walkthrough_smoke
```

For the human session, use the separate interactive node and command below;
changing the mode value alone leaves the smoke test selected and will fail the
required-execution check. Type `exit` or press Ctrl-D at the shell to close it.
The launcher prints the shell's exit status, including nonzero exits, then
closes its local HTTPS server and removes its temporary wheel, token, key, and
export files. Pytest fixture teardown drops only that run's PostgreSQL database
and stops only its uniquely named disposable container. A forced process kill
can bypass fixture teardown, so it is not a supported stop method.

For the actual U2 candidate, set the verified asset root only on the
interactive command:

```bash
VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes \
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
VONK_QWEN3_CPU_ASSET_ROOT=/private/tmp/vonk-cli-u2-qwen-assets \
VONK_WALKTHROUGH_MODE=interactive \
PYTHONPATH="$PWD:$PWD/src:$PWD/control/src" \
uv run --project control --frozen --with-editable . \
  pytest -p control.tests.required_execution -q -s \
  control/tests/test_cli_operator_walkthrough.py::test_disposable_cli_operator_walkthrough_interactive
```

The option is used only in interactive mode; smoke mode stays on the default
synthetic fixture. Skopeo and the pinned local assets are required for the Qwen
session. The external asset directory must contain `hub-snapshot/`
from Qwen/Qwen3-0.6B commit
`c1899de289a04d12100db370d81485cdf75e47ca`, including the pinned
`model.safetensors` file (1,503,300,328 bytes, SHA256
`f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b`), and
`vllm-qwen3-cpu-runtime-verified-u2-20260924.tar` (881,898,496 bytes, SHA256
`d7af73ab4387d996caaf7b74eea0e45886a351cb9a2cf9014f42c6cd4025516d`). The
runtime wrapper is derived from
`docker.io/vllm/vllm-openai-cpu@sha256:527ec4e8188f2ad480aca5863ab3b7e7c39cfda84f6c0bbb06525363a3eb5a0f`;
its Skopeo-inspected archive manifest is
`sha256:c775a5f6e778c53946ce3dc266dc4052d3ca1e18f904b4d09eed8cef1c0489ee`.
The interactive launcher validates these pins and prepares managed cache
records, but does not start inference or submit a run. Run the separate Qwen
CPU smoke before the human session; it uses the same asset-root variable to
verify the blocked-row preparation request and local inference:

```bash
VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes \
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
VONK_QWEN3_CPU_ASSET_ROOT=/private/tmp/vonk-cli-u2-qwen-assets \
VONK_QWEN3_CPU_SMOKE=1 \
PYTHONPATH="$PWD:$PWD/src:$PWD/control/src" \
uv run --project control --frozen --with-editable . \
  pytest -p control.tests.required_execution -q -s \
  control/tests/test_cli_qwen3_cpu_walkthrough.py::test_installed_cli_later_page_qwen_candidate_runs_real_cpu_inference
```

The shell has ordinary host networking; this is not an OS network sandbox. The
configured Controller URL points to a loopback HTTPS peer and its short-lived
credential is signed by a random key held only by the disposable app. Do not
replace that URL or provide a production credential. The Profile-load route
and `FleetProfileService` are wired, but the provider refuses a load with HTTP
409; no Profile-load work is dispatched. Fleet removal and upgrade providers
are not registered. The fixture starts no workers or Spark network target.
Profile save/export/import changes only the disposable SQL owner and files.

Before scheduling a participant, prepare a disposable environment with the
candidate's independently installed wheel, registered HTTPS API, PostgreSQL,
managed storage, and authorized test identities. No token may appear in shell
history, transcripts, or scorecards. The participant must receive a regular
private token file, not a token argument. Keep reset/recovery procedures with
the facilitator. Never use production data or non-disposable Spark workloads.

Seed the following boundaries and confirm they work before the session:

| Cards | Facilitator prerequisites |
| --- | --- |
| U1–U2 | Offline help; seeded `invalid-connection.env` and a valid-but-expired token file (the launcher prints both paths); an authorized Fleet identity; a multi-page recipe catalog, a later-page runnable Qwen candidate, and a Fleet-fit-but-cache-blocked candidate with an exact blocker. The default synthetic smoke's queued preparation request is separate evidence and does not describe the Qwen row. Skopeo and pinned Qwen assets are required for the Qwen session; physical Spark execution remains unproven. |
| U3 | An unused Profile number and a safe export/import destination; the scenario must preserve metadata and installed-only state without dispatching work. |
| U4 | A whole-Fleet scenario with affected and idle Sparks; a controllable stale-review change between review and acceptance; a noninteractive invocation that requires the exact reviewed decision and explicit consent. |
| U5 | An accepted application that remains observably queued after local follow is interrupted, so the participant can distinguish observer loss from remote failure; a new shell must query the same application ID while a newer application exists. |
| U6 | A durable blocker with an advertised action; a cancellation scenario with issued and unissued effects, a shared or reusable verified asset, and truthful settlement after restart. |
| U7 | A Profile-owned current published route; a succeeded artifact job with verified files; and an unavailable or empty-result case that distinguishes those states. |
| U8 | Disposable cleanup and upgrade targets, an authorized package source, a safe first-target failure case, a read-only JSON command with a pipe consumer, and predictable stdout/stderr/exit behavior without prompts. Cleanup and upgrade run in separate fixtures; record the pipe in the upgrade shell. Check the [current W19 package row](cli-operator-status.md#package-state) before scheduling. |

## Host prerequisites and launch order

Run the fixtures from the task worktree root with `uv`, the sibling recipe
checkout, and a writable task-specific `UV_CACHE_DIR`. The launch commands use
`VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes` and the frozen Control
environment. That variable is read by the walkthrough fixture; confirm it
resolves to the intended checkout before starting.

Every PostgreSQL facilitator needs Docker. Before the session, run
`docker context show` and `docker info`; on macOS, both must identify the
intended OrbStack engine. If needed, select `orbstack` as described in the
[testing policy](../testing-and-ci.md#local-linux-and-container-testing), then
repeat the checks. The PostgreSQL fixture can skip when no engine is available,
so a green-looking collection with no executed test is not acceptance evidence.
Every documented acceptance command uses
`-p control.tests.required_execution`; a selected skip, xfail, or zero executed
tests makes that command fail. Run one exact node ID per opt-in mode, with its
matching `VONK_*_MODE` value. Do not run a whole mode-gated file and count the
other mode's intentional skips as passes.

Skopeo is also required for the Qwen asset inspection and Qwen CPU smoke. A
missing Skopeo executable causes a skip and therefore a failed required
acceptance command. It is not a pass. Physical NVIDIA/Spark or model-quality
claims still need their designated evidence.

Use this selector form for the connected tests listed below and in the linked
facilitator guides:

```bash
PYTHONPATH="$PWD:$PWD/src:$PWD/control/src" \
VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes \
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
uv run --project control --frozen --with-editable . \
  pytest -p control.tests.required_execution -q -s <exact-test-node-id>
```

Run the pre-session smoke and connected fault selections first, in card order;
keep them as automated evidence. Then conduct the human tasks in this order.
Each linked facilitator guide has the exact smoke and interactive node IDs.

| Order | Human task | Interactive setup and selected node |
| --- | --- | --- |
| 1 | U1–U3 | This document's Qwen-backed interactive command: `test_cli_operator_walkthrough.py::test_disposable_cli_operator_walkthrough_interactive`. The Qwen assets make the runnable later-page candidate available for U2. |
| 2 | U4 | [Whole-Fleet facilitator](cli-u4-whole-fleet-facilitator.md): `test_cli_fleet_load_walkthrough.py::test_installed_cli_u4_disposable_human_session`. |
| 3 | U5 | [Observer setup](cli-observer-walkthrough.md): `test_cli_observer_walkthrough.py::test_disposable_cli_observer_walkthrough_interactive`. |
| 4 | U6 | [Cancellation setup](cli-cancellation-walkthrough.md): `test_cli_cancellation_walkthrough.py::test_disposable_cli_cancellation_walkthrough_interactive`. |
| 5 | U7 | [Results setup](cli-results-walkthrough.md): `test_cli_results_walkthrough.py::test_disposable_cli_results_walkthrough_interactive`. The connected same-Profile journey smoke is a separate W19 gate. |
| 6 | U8 | Run the [read-only pipeline smoke](cli-operator-u8-readonly.md) before the session. In the [upgrade setup](cli-upgrade-walkthrough.md) shell, record the participant's pipe and sequential-upgrade task; then use the separate [cleanup setup](cli-cleanup-walkthrough.md) shell for cleanup review. Record each U8 subtask. |

Give the same participant only the shipped runbook copy and one outcome card
at a time. Keep source, tests, facilitator setup, expected outcomes, and command
sequences out of the materials they receive. The shell itself does not enforce
those restrictions, so record any source/test access, outside research, or hint
as assisted evidence. The retained runbook copy is the same participant-facing
material for U4–U8; those fixtures remove their temporary workspaces at teardown
and do not provision it.

The existing W19 plan permits a deterministic executor to isolate
control-plane behavior, but it must exercise the registered API and service
owners. A synthetic response, fake CLI client, or manually inserted “success”
is not a substitute. Deterministic acceptance does not establish physical
Spark, model quality, publication, or deployment behavior.

## Facilitator-only expected outcomes

| ID | Evidence to retain; failure conditions |
| --- | --- |
| U1 | Offline help succeeds; invalid access names the failed boundary; valid access completes the named Fleet read; no credential appears in output. Secret exposure or an auth bypass blocks handoff. |
| U2 | The participant reaches the exact usable later-page candidate, separates fit from readiness, and names the blocker and current prepare action for the missing-asset candidate. A first-page-only answer or guessed cache readiness fails. A queued preparation request alone does not prove a usable candidate. |
| U3 | Canonical authoring fields survive edit/export/import; installed-only intent remains installed; no job, run, or route changes as a side effect of saving. |
| U4 | The review names complete targets and effects. A stale decision is refused before dispatch and is not silently resubmitted; the noninteractive path requires and binds the exact reviewed digest and consent. |
| U5 | Interruption or timeout changes only local observation. A fresh process follows the original application ID, not “latest”; the last remote result remains distinct from the observation outcome. |
| U6 | Only an advertised owner action is used. Cancellation keeps issued effects visible until reconciled, does not cancel another consumer's work, and preserves already verified usable assets. |
| U7 | Endpoint output is current, Profile-scoped, and credential-free. Artifact files are published only after integrity verification. Unavailable output remains distinct from an empty successful result. |
| U8 | Cleanup scope is explained before any permitted mutation; upgrades stop after the first failure instead of dispatching to later Sparks. A safe JSON pipeline emits one result, has the documented exit, and does not prompt. |

Stop the session on a wrong-target mutation, exposed secret, changed or
unreviewed effect, duplicate operation, false success, missing verified-file
check, inability to distinguish local observer failure from remote state, or
failure to discover or complete any core task. Do not coach through a failure.
Record it, correct the underlying issue, and schedule a fresh attempt for the
affected task.

## Scorecard

Use one row per participant and task. Record participant role and prior
familiarity with Vonk, `vonkctl`, CLI operations, and repository/source access.
Record wall-clock elapsed time, active operator time, and transfer/build/
execution wait separately. A documentation lookup is allowed but must be
recorded; an implementer's hint is not an unassisted pass. Record the exact
unclear phrase and the participant's interpretation. Evidence references must
point only to sanitized files; redact credentials, authorization headers,
token-bearing URLs, private keys, certificates, and personal data before saving
or linking any transcript.

| Participant, role, prior familiarity / source access | Task | Completed without hints? | Docs, outside research, or hints | Wrong commands / targets | Unclear term and interpretation | Manual JSON needed? | Wall-clock elapsed | Active operator time | Transfer / build / execution wait | Sanitized evidence reference |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  | U1 |  |  |  |  |  |  |  |  |  |
|  | U2 |  |  |  |  |  |  |  |  |  |
|  | U3 |  |  |  |  |  |  |  |  |  |
|  | U4 |  |  |  |  |  |  |  |  |  |
|  | U5 |  |  |  |  |  |  |  |  |  |
|  | U6 |  |  |  |  |  |  |  |  |  |
|  | U7 |  |  |  |  |  |  |  |  |  |
|  | U8 |  |  |  |  |  |  |  |  |  |

## Correction and retest record

Keep one entry for each failed or assisted core task. Retest only after the
underlying correction is available, using a fresh attempt. A coached retry does
not replace the original unassisted result.

| Participant / task | Initial failure and evidence | Correction and revision | Fresh retest node / date | Retest outcome and evidence |
| --- | --- | --- | --- | --- |
|  |  |  |  |  |

W19 remains open until each of the thirteen connected rows below has recorded
evidence, all eight human cards have independent participant results, and any
dangerous or blocked core task has been corrected and retested. The eight-card
scorecard does not stand in for the thirteen technical gates. This walkthrough
is qualitative usability evidence, not a statistical study. It cannot close
W09/W17 or replace the separate Linux/PostgreSQL, publication, deployment, or
physical-acceptance gates. Consult the [current W19 package row](cli-operator-status.md#package-state)
before scheduling; record human results separately from automated status.

## W19 connected-scenario evidence map

The selectors below identify existing tests; none is a participant result.
Run a non-mode-gated selector with the command form above. For an opt-in smoke
or interactive selector, set its matching `VONK_*_MODE` value and run only the
exact node named here or in its facilitator guide. The required-execution plugin
must be used in every documented acceptance command. Current pass/fail evidence
belongs in the [W19 package row](cli-operator-status.md#package-state), with its
revision and command recorded.

| W19 connected scenario | Human task | Existing evidence selectors | Evidence scope and remaining seam |
| --- | --- | --- | --- |
| First connection | U1 | `control/tests/test_cli_operator_walkthrough.py::test_disposable_cli_operator_walkthrough_smoke`; `control/tests/test_cli_first_connection_endpoints_installed.py::test_installed_cli_first_connection_requires_a_valid_controller_token`; `control/tests/test_cli_runbook_parser_installed.py::test_runbook_vonkctl_examples_parse_in_the_installed_wheel` | The walkthrough smoke covers seeded invalid origin, expired token, and authorized Fleet read. The independent human result remains required. |
| Find and prepare | U2 | `control/tests/test_cli_operator_walkthrough.py::test_disposable_cli_operator_walkthrough_smoke`; `control/tests/test_find_prepare_installed_cli.py::test_installed_cli_finds_later_page_missing_asset_and_accepts_exact_cache_operation`; `control/tests/test_cli_qwen3_cpu_walkthrough.py::test_installed_cli_later_page_qwen_candidate_runs_real_cpu_inference` | The first two use the synthetic missing-model preparation row; the Qwen test binds the exact Qwen model, queues its missing-image action, and checks local inference. Neither establishes physical Spark or model-quality acceptance. |
| Preserve authoring | U3 | `control/tests/test_profile_authoring_installed_cli.py::test_installed_profile_edit_preserves_definition_and_rejects_concurrent_save`; `control/tests/test_cli_operator_walkthrough.py::test_disposable_cli_operator_walkthrough_smoke` | Installed-only authoring and stale/concurrent save refusal are automated. A human must still explain the save boundary. |
| Review and load | U4 | `control/tests/test_cli_fleet_load_walkthrough.py::test_installed_cli_u4_whole_fleet_stale_and_exact_scripted_consent`; `control/tests/test_profile_load_review_installed_cli.py::test_installed_cli_reviews_real_whole_fleet_effects_before_prompt`; `control/tests/test_profile_load_stale_admission_installed_cli.py::test_installed_stale_review_is_shown_and_never_admitted_or_replayed`; `control/tests/test_profile_load_installed_cli.py::test_installed_interactive_review_recovers_the_original_load_after_edit` | Covers affected/idle review, stale refusal, and explicit exact-digest consent. The separate U4 human shell records discovery. |
| Ambiguous submit | No human card; separate fault gate | `control/tests/test_profile_load_submission.py::test_cli_recovers_committed_load_after_lost_response_and_profile_edit`; `control/tests/test_profile_load_closed_pipe.py::test_closed_stdout_reconnects_to_the_single_accepted_load` | The tests inject loss after commit and reconcile the original request. U5's local-follow interruption is a different failure boundary and does not cover this row. |
| Recipe update batch | No human card; separate fault gate | `control/tests/test_recipe_update_batch_installed_cli.py::test_installed_update_survives_cli_and_worker_death_with_frozen_cache_scope` | Covers response/process loss, restart adoption, and the frozen recipe scope through the installed CLI. No human card injects this parent/child failure. |
| Reconnect | U5 | `control/tests/test_cli_observer_walkthrough.py::test_disposable_cli_observer_walkthrough_smoke`; `control/tests/test_profile_follow_interrupt_installed.py::test_installed_follow_sigint_retains_exact_application_without_remote_mutation`; `control/tests/test_profile_follow_interrupt_installed.py::test_installed_follow_timeout_stays_with_original_application_after_newer_load` | The disposable observer fixture retains queued remote state while a fresh process follows the original application ID. The participant must distinguish that from remote failure. |
| Recover storage | No human card; separate fault gate | `control/tests/test_profile_build_process_recovery.py::test_profile_recovers_after_worker_process_death` (all ten parameter cases); `control/tests/test_model_cache.py::test_interrupted_download_checkpoint_resumes_after_service_restart`; `control/tests/test_model_cache.py::test_stored_bytes_without_a_receipt_are_not_admitted`; `control/tests/test_model_cache.py::test_same_pin_repair_verifies_before_atomic_replace_and_preserves_old_bytes` | PostgreSQL worker recovery removes an accepted image archive/receipt, then checks restart and refuses changed replacement identity. Model-cache tests cover partial resume and receipt/repair integrity. The route-backed preparation request and worker/storage recovery are separate test seams; do not claim one test joins them or that this is a human result. |
| Cancel | U6 | `control/tests/test_cli_cancellation_walkthrough.py::test_disposable_cli_cancellation_walkthrough_smoke`; `control/tests/test_profile_cancel_activity_installed_cli.py::test_installed_profile_cancel_recovery_is_visible_in_activity`; `control/tests/test_profile_cancel_activity_installed_cli.py::test_installed_cancel_reports_issued_child_until_late_receipt_is_reconciled`; `control/tests/test_recipe_cancel_installed_cli.py::test_installed_recipe_cancel_recovers_dropped_acceptance_and_settles`; `control/tests/test_model_cancel_installed_cli.py::test_installed_model_cancel_recovers_lost_receipt_and_observes_settlement` | Covers cancellation recovery and exact issued/unissued effects. The interactive session and its human observations are separate. |
| Serving result | U7 | `control/tests/test_cli_linked_profile_journey.py::test_installed_cli_links_profile_import_load_progress_and_published_endpoint`; `control/tests/test_cli_results_walkthrough.py::test_disposable_cli_results_walkthrough_smoke`; `control/tests/test_cli_first_connection_endpoints_installed.py::test_registered_profile_endpoint_binds_database_owner_alias_and_generation`; `control/tests/test_cli_first_connection_endpoints_installed.py::test_installed_cli_discovers_only_the_published_profile_endpoint_and_revocation` | Confirms current Profile/route identity and credential-free output through local owners. No fixture serves a physically running model. |
| Artifact result | U7 | `control/tests/test_cli_results_walkthrough.py::test_disposable_cli_results_walkthrough_smoke`; `control/tests/test_artifact_job_installed_cli.py::test_installed_cli_recovers_submitted_job_and_publishes_only_verified_output`; `control/tests/test_artifact_job_installed_cli.py::test_installed_cli_distinguishes_unavailable_from_empty_result_manifest` | Covers verified file retrieval, unavailable output, and empty success. Deterministic result bytes are not Spark execution. |
| Maintenance | U8 | `control/tests/test_cli_cleanup_walkthrough.py::test_disposable_u8_cleanup_walkthrough_smoke`; `control/tests/test_cli_upgrade_walkthrough.py::test_disposable_u8_upgrade_walkthrough_smoke`; `control/tests/test_fleet_remove_installed_cli.py::test_installed_fleet_remove_requires_consent_and_uses_canonical_node`; `control/tests/test_fleet_upgrade_installed_cli.py::test_installed_upgrade_reconnects_to_first_failure_without_dispatching_next_spark` | Cleanup and upgrade use separate disposable fixtures. Smoke success is not a human explanation or production mutation authorization. |
| Automation | U8 | `control/tests/test_cli_json_pipeline_installed.py::test_installed_no_input_fleet_json_pipeline_is_read_only`; `control/tests/test_profile_load_closed_pipe.py::test_closed_stdout_reconnects_to_the_single_accepted_load`; `control/tests/test_profile_load_installed_cli.py::test_installed_json_no_input_load_requires_reviewed_digest_and_emits_one_result` | Covers a read-only JSON pipe, a closed output pipe after accepted work, and exact noninteractive consent. The operator must still demonstrate predictable output/exit behavior without a hint. |

This inventory includes the smoke and interactive instruments themselves,
including the linked-profile journey, Qwen CPU setup, and every U4–U8
facilitator. The human U1–U8 scorecard is still unperformed.

## Connected gate run record

Copy this table for each acceptance run. For every row, record the source and
recipe revisions, exact selected nodes and mode, injected fault, exit status,
executed/pass/fail/skip counts, and sanitized evidence location. Use the map
above to select the evidence; a test name alone is not a result. Blank rows,
skips, and unresolved seams cannot be counted as passed. The three scenarios
without a human card are owned and recorded here by the acceptance facilitator.
Existing automated results are retained in the
[correction checkpoint](cli-operator-status.md#acceptance-review-corrections--2026-09-24);
copy only results whose stated revision and scope match the acceptance claim.

| Scenario | Source / recipes / selected nodes / mode | Injected boundary | Exit and executed / passed / failed / skipped | Sanitized evidence and remaining gap |
| --- | --- | --- | --- | --- |
| First connection | | | | |
| Find and prepare | | | | |
| Preserve authoring | | | | |
| Review and load | | | | |
| Ambiguous submit | | | | |
| Recipe update batch | | | | |
| Reconnect | | | | |
| Recover storage | | | | |
| Cancel | | | | |
| Serving result | | | | |
| Artifact result | | | | |
| Maintenance | | | | |
| Automation | | | | |
