# W19 operator walkthrough protocol

**Status:** no human participant acceptance has been performed. A disposable
U1/U3 fixture and U2 cache-readiness setup are available. The empty image
fixture does not establish a runnable candidate. This protocol does not prove
that an operator completed the CLI journey and does not close W19.

The source criteria are the [W19 implementation plan](cli-operator-implementation.md#operator-walkthrough-and-acceptance-scorecard).
The participant receives only the shipped [`vonkctl` runbook](../runbooks/vonkctl.md),
the installed executable, connection details, and the outcome-only task cards
below. The participant must not be shown facilitator setup, expected outcomes,
tests, source, or a command sequence. They may use the shipped runbook; record
each lookup and any request for help. Use a regular operator and, when
available, a CLI-literate operator unfamiliar with the implementation.

## Outcome-only task cards

Give the participant these tasks verbatim, one at a time. Do not demonstrate a
command or tell them which runbook section to use.

| ID | Task to give the participant |
| --- | --- |
| U1 | Find help while offline. Identify a deliberately invalid Controller connection, then complete a named Fleet read with authorized access without exposing the credential. |
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
perform cleanup or upgrades on production resources.

## Facilitator setup — do not give to participants

The opt-in [`test_cli_operator_walkthrough.py`](../../control/tests/test_cli_operator_walkthrough.py)
provides a disposable first-connection, recipe-discovery, and Profile-authoring
setup. Its smoke mode exercises an independently built CLI wheel, the
registered HTTPS API, and PostgreSQL-backed catalog, Fleet, cache-assessment,
recipe-preparation, and Profile owners. Its interactive mode opens a local
shell against those same services. U2 has three Fleet-fit pages: the middle
page contains a cache-ready candidate, prepared through `ModelCacheService`
with one digest-pinned local model fixture and synchronous owner processing;
the final page contains a separate cache-blocked candidate with its exact
advertised download action. The blocked candidate's action creates a real
PostgreSQL-backed queued preparation request. The model source is served only
by a request-restricted local `httpx.MockTransport`; a valid local OCI layout
is converted and inspected by Skopeo before the image digest is pinned and
published through `RecipeImageAvailabilityService` and managed storage. Setup
files stay under the walk-through temporary directory. The fixture runs no
background worker and fetches no external asset or registry image. Its ready
projection proves current fit and cache readiness only; it does not prove that
the empty OCI fixture starts in an engine, that model bytes have useful quality,
or that a Spark can run it. If Skopeo is missing, the test reports an explicit
skip rather than a successful ready-candidate result. U4–U8 operational
scenarios remain unavailable in this launcher. Separate [U4 whole-Fleet review](cli-u4-whole-fleet-facilitator.md),
[U5 observer setup](cli-observer-walkthrough.md), [U6 cancellation setup](cli-cancellation-walkthrough.md),
and [U7 results setup](cli-results-walkthrough.md) exercise stale-review consent,
exact-ID reconnect, cancellation across worker restart, and result retrieval. The [U8 read-only pipeline](cli-operator-u8-readonly.md)
checks actual process composition. The [U8 upgrade setup](cli-upgrade-walkthrough.md)
provides a separate disposable shell with two test nodes and a controlled first-target
failure through the real job owner. The [U8 cleanup setup](cli-cleanup-walkthrough.md)
adds a disposable review, consent and removal scenario; its integrated smoke
qualification is recorded in the current status document. W19 stays open and
starting any shell does not imply a participant result.

From the repository root, launch the noninteractive connection/discovery/authoring smoke with:

```bash
VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes \
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache \
VONK_WALKTHROUGH_MODE=smoke \
uv run --project control --frozen --with-editable . \
  pytest -q -s control/tests/test_cli_operator_walkthrough.py
```

Replace `smoke` with `interactive` to open the human session. Type `exit` or
press Ctrl-D at the shell to close it.
The launcher prints the shell's exit status, including nonzero exits, then
closes its local HTTPS server and removes its temporary wheel, token, key, and
export files. Pytest fixture teardown drops only that run's PostgreSQL database
and stops only its uniquely named disposable container. A forced process kill
can bypass fixture teardown, so it is not a supported stop method.

The shell has ordinary host networking; this is not an OS network sandbox. The
configured Controller URL points to a loopback HTTPS peer and its short-lived
credential is signed by a random key held only by the disposable app. Do not
replace that URL or provide a production credential. The app has no registered
Fleet removal, upgrade, or Profile-load service and starts no workers or Spark
network target. Profile save/export/import changes only the disposable SQL
owner and files.

Before scheduling a participant, prepare a disposable environment with the
candidate's independently installed wheel, registered HTTPS API, PostgreSQL,
managed storage, and authorized test identities. No token may appear in shell
history, transcripts, or scorecards. The participant must receive a regular
private token file, not a token argument. Keep reset/recovery procedures with
the facilitator. Never use production data or non-disposable Spark workloads.

Seed the following boundaries and confirm they work before the session:

| Cards | Facilitator prerequisites |
| --- | --- |
| U1–U2 | Offline help; one invalid origin or denied/expired token case; an authorized Fleet identity; a recipe catalog with more than one page, a later-page runnable candidate, a separate Fleet-fit-but-cache-blocked candidate, and an actionable missing-asset reason. The fixture now provides verified cache bytes and a ready control-plane assessment, but its empty OCI image does not establish the runnable-candidate prerequisite. Engine startup and Spark execution remain unproven. |
| U3 | An unused Profile number and a safe export/import destination; the scenario must preserve metadata and installed-only state without dispatching work. |
| U4 | A whole-Fleet scenario with affected and idle Sparks; a controllable stale-review change between review and acceptance; a noninteractive invocation that requires the exact reviewed decision and explicit consent. |
| U5 | An accepted application that remains observable long enough to interrupt local follow; a new shell must be able to query the same application ID while a newer application can exist. |
| U6 | A durable blocker with an advertised action; a cancellation scenario with issued and unissued effects, a shared or reusable verified asset, and truthful settlement after restart. |
| U7 | A Profile-owned current published route; a succeeded artifact job with verified files; and an unavailable or empty-result case that distinguishes those states. |
| U8 | Disposable cleanup and upgrade targets, an authorized package source, a safe first-target failure case, and a read-only JSON command suitable for a pipe. Use the dedicated disposable cleanup fixture and check its integrated qualification in the current status record before scheduling a participant. |

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
check, or inability to distinguish local observer failure from remote state.
Do not coach through a failure; retain its evidence, correct the underlying
issue, and schedule a fresh attempt.

## Scorecard

Use one row per participant and task. Record wall-clock task time separately
from transfer, build, and execution waiting time. A documentation lookup is
allowed but must be recorded; an implementer's hint is not an unassisted pass.

| Participant / role | Task | Complete without hints? | Docs looked up / hints | Wrong commands or targets | Manual JSON needed? | Active time / work wait | Sanitized evidence reference |
| --- | --- | --- | --- | --- | --- | --- | --- |
|  | U1 |  |  |  |  |  |  |
|  | U2 |  |  |  |  |  |  |
|  | U3 |  |  |  |  |  |  |
|  | U4 |  |  |  |  |  |  |
|  | U5 |  |  |  |  |  |  |
|  | U6 |  |  |  |  |  |  |
|  | U7 |  |  |  |  |  |  |
|  | U8 |  |  |  |  |  |  |

W19 remains open until all eight cards and the connected acceptance gates pass,
the independent participant results are recorded, and any dangerous or blocked
core task has been corrected and retested. This walkthrough is qualitative
usability evidence, not a statistical study. It cannot close W09/W17 or replace
the separate Linux/PostgreSQL, publication, deployment, or physical-acceptance
gates. Consult the [current status record](cli-operator-status.md) before
scheduling; package gates can change independently of this protocol.

## Existing automated evidence

These tests cover automated process or registered-route boundaries; none fills
the scorecard above:

- First connection, endpoint ownership, and runbook parsing:
  [`test_cli_first_connection_endpoints_installed.py`](../../control/tests/test_cli_first_connection_endpoints_installed.py),
  [`test_cli_runbook_parser_installed.py`](../../control/tests/test_cli_runbook_parser_installed.py).
- Profile load, stale review, authoring, exact reconnect, and cancellation:
  [`test_profile_load_installed_cli.py`](../../control/tests/test_profile_load_installed_cli.py),
  [`test_profile_load_stale_admission_installed_cli.py`](../../control/tests/test_profile_load_stale_admission_installed_cli.py),
  [`test_profile_authoring_installed_cli.py`](../../control/tests/test_profile_authoring_installed_cli.py),
  [`test_profile_follow_interrupt_installed.py`](../../control/tests/test_profile_follow_interrupt_installed.py),
  [`test_profile_cancel_activity_installed_cli.py`](../../control/tests/test_profile_cancel_activity_installed_cli.py).
- Find-and-prepare, durable recipe updates, artifact jobs, and cancellation:
  [`test_find_prepare_installed_cli.py`](../../control/tests/test_find_prepare_installed_cli.py),
  [`test_recipe_update_batch_installed_cli.py`](../../control/tests/test_recipe_update_batch_installed_cli.py),
  [`test_artifact_job_installed_cli.py`](../../control/tests/test_artifact_job_installed_cli.py),
  [`test_recipe_cancel_installed_cli.py`](../../control/tests/test_recipe_cancel_installed_cli.py),
  [`test_model_cancel_installed_cli.py`](../../control/tests/test_model_cancel_installed_cli.py).
- Fleet removal consent, resume, and sequential upgrade:
  [`test_fleet_remove_installed_cli.py`](../../control/tests/test_fleet_remove_installed_cli.py),
  [`test_fleet_resume_installed_cli.py`](../../control/tests/test_fleet_resume_installed_cli.py),
  [`test_fleet_upgrade_installed_cli.py`](../../control/tests/test_fleet_upgrade_installed_cli.py).
