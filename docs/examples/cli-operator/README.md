# Actual installed CLI examples

Source checkpoint: `872d639aa48da2aabbe5d67676a15ed21a548da6`.
Captured: `2026-09-24T05:14:35.114562+00:00`. Seven installed acceptance tests passed; the
[complete capture](capture.json) retains all 32 observed commands, exits and
streams. These are disposable PostgreSQL/HTTPS service-owner scenarios with
a freshly installed CLI wheel. Deterministic fixtures establish neither human
usability nor deployment, publication, engine startup or physical Spark behavior.

## JSON examples

| File | Meaning | Capture record (zero-based) |
| --- | --- | --- |
| [review.json](review.json) | Idle profile review: no workload effects | 1 |
| [receipt.json](receipt.json) | Accepted idle profile: no-effect success | 4 |
| [progress.json](progress.json) | Cancellation with issued effects still pending | 9 |
| [refusal.json](refusal.json) | Definitive artifact submission refusal | 14 |
| [verified-download.json](verified-download.json) | Verified nonempty artifact download | 17 |
| [empty-result.json](empty-result.json) | Successful contract-valid empty result | 23 |

The JSON files preserve captured values and document structure; only whitespace
is pretty-printed. The full capture retains the original JSON line. Fixture
paths are replaced with `/disposable-fixture`, and the executable is named
`vonkctl`. IDs and digests are unchanged. Commands use disposable identities
and are examples to inspect, not commands to copy against another Controller.

## Human output

The following are complete captured streams for the named observations.
Terminal carriage returns are displayed as ordinary line endings here; the
complete capture retains them. Empty streams are omitted.

### Whole-Fleet review, operator declines

Capture record `0`; exit `2`.

```sh
vonkctl --profile 1 profile load --detach
```

terminal:

```text
Ready for review
Profile: Studio replacement
Revision: 1
Plan digest: 392dca153b787d191a010ac5a867ab85b62d2e73e1faa3ef88e7f7985babb623
All Sparks: spk_00000000000000000000000000000001, spk_00000000000000000000000000000002, spk_33333333333333333333333333333333
Idle Sparks: spk_33333333333333333333333333333333
Already Correct: 0
Placements: 0
Builds: 0
Distributions: 0
Installs: 0
Starts: 1
Stops: 1
Uninstalls: 0
Blockers: 0
Recipe: Synthetic Tiny image
Sparks: spk_00000000000000000000000000000001, spk_00000000000000000000000000000002
Current: degraded
Desired: running
0. Switch profile Studio replacement
Affected Sparks: spk_00000000000000000000000000000001, spk_00000000000000000000000000000002
Stop endpoint: studio-chat
Run: 2090cf27-5561-4a7e-95d6-d30b25ad0be6
Complete group: spk_00000000000000000000000000000001, spk_00000000000000000000000000000002
Keep installation: c59afb7a-5262-4c6c-ba8a-876ddc3f20a5
Complete group: spk_00000000000000000000000000000001, spk_00000000000000000000000000000002
Assignment: 756cf4c0-cce5-5c9f-858a-0edd415ead81
Exact model set: ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
Exact image: sha256:1111111111111111111111111111111111111111111111111111111111111111
OCI archive SHA-256: fc3674abb069b349caf8573256160eb1be4ccc4851e69b14873662f3f189e191
Image size: 30 B
Architecture: linux-arm64
Runtime interface: vonk.runtime.v1
Build: 4277404f-3f50-4444-858b-ba783438dd51
Reuse model on: spk_00000000000000000000000000000001, spk_00000000000000000000000000000002
Reuse image on: spk_00000000000000000000000000000001, spk_00000000000000000000000000000002
Admission for assignment: 756cf4c0-cce5-5c9f-858a-0edd415ead81
Intended endpoint: studio-chat
Current capacity: blocked
Spark: spk_00000000000000000000000000000001
Ports required: 8000, 29500
Memory demand kind: unified
Physical memory pool: shared
Memory required: 225 B
Memory reserve: 50 B
Physical memory capacity: 9.8 KiB (10000 bytes)
Available in limiting pool: 7.8 KiB (8000 bytes)
Memory after placement: 7.4 KiB (7550 bytes)
Disk required: 1.1 KiB (1084 bytes)
Disk after placement: 5.7 KiB (5872 bytes)
Attention: spk_00000000000000000000000000000001 capacity blocker: run.port_occupied: Port 8000 is already reserved on this GPU node.
Attention: spk_00000000000000000000000000000001 capacity blocker: run.rendezvous_port_occupied: Multi-node rendezvous port 29500 is already reserved.
Spark: spk_00000000000000000000000000000002
Ports required: 8000
Memory demand kind: unified
Physical memory pool: shared
Memory required: 225 B
Memory reserve: 50 B
Physical memory capacity: 9.8 KiB (10000 bytes)
Available in limiting pool: 7.8 KiB (8000 bytes)
Memory after placement: 7.4 KiB (7550 bytes)
Disk required: 1.1 KiB (1084 bytes)
Disk after placement: 5.7 KiB (5872 bytes)
Attention: spk_00000000000000000000000000000002 capacity blocker: run.port_occupied: Port 8000 is already reserved on this GPU node.
Capacity after stops: fits
Spark: spk_00000000000000000000000000000001
Ports required: 8000, 29500
Memory demand kind: unified
Physical memory pool: shared
Memory required: 225 B
Memory reserve: 50 B
Physical memory capacity: 9.8 KiB (10000 bytes)
Available in limiting pool: 7.8 KiB (8000 bytes)
Memory after placement: 7.4 KiB (7550 bytes)
Disk required: 1.1 KiB (1084 bytes)
Disk after placement: 5.7 KiB (5872 bytes)
Spark: spk_00000000000000000000000000000002
Ports required: 8000
Memory demand kind: unified
Physical memory pool: shared
Memory required: 225 B
Memory reserve: 50 B
Physical memory capacity: 9.8 KiB (10000 bytes)
Available in limiting pool: 7.8 KiB (8000 bytes)
Memory after placement: 7.4 KiB (7550 bytes)
Disk required: 1.1 KiB (1084 bytes)
Disk after placement: 5.7 KiB (5872 bytes)
Attention: run-switch.model_capabilities_unknown: The exact model revision declares no typed capability facts.
Preparation: 756cf4c0-cce5-5c9f-858a-0edd415ead81
Controller ready: yes
Targets ready: yes
Attention: profile.interruption_expected: The reviewed plan includes required runtime stops; affected workloads may be unavailable until final starts complete.
Load profile 1 with these effects? [y/N] no
Error: action was not confirmed
Code: control.api_error
Detail: action was not confirmed
```

### Stale decision refused and current review displayed

Capture record `2`; exit `2`.

```sh
vonkctl --profile 1 profile load --expected-plan fe70c58845e90a866e018391222fb7cb8702a997b405da0ddea8eab69c4b2caf --request-key 11111111-1111-4111-8111-111111111111 --detach
```

terminal:

```text
Ready for review
Profile: Reviewed idle
Revision: 1
Plan digest: fe70c58845e90a866e018391222fb7cb8702a997b405da0ddea8eab69c4b2caf
All Sparks: spk_11111111111111111111111111111111, spk_22222222222222222222222222222222
Idle Sparks: spk_11111111111111111111111111111111, spk_22222222222222222222222222222222
Already Correct: 0
Placements: 0
Builds: 0
Distributions: 0
Installs: 0
Starts: 0
Stops: 0
Uninstalls: 0
Blockers: 0
Load profile 1 with reviewed plan fe70c58845e90a866e018391222fb7cb8702a997b405da0ddea8eab69c4b2caf? [y/N] yes
Request key: 11111111-1111-4111-8111-111111111111
Reconnect: vonkctl --profile 1 profile progress --request-key 11111111-1111-4111-8111-111111111111 --follow
Ready for review
Profile: Edited after review
Revision: 2
Plan digest: fc6e50152ecb0fffee469629c52eb7f404732d40ff04c3d5108359e5e93c55d9
All Sparks: spk_11111111111111111111111111111111, spk_22222222222222222222222222222222
Idle Sparks: spk_11111111111111111111111111111111, spk_22222222222222222222222222222222
Already Correct: 0
Placements: 0
Builds: 0
Distributions: 0
Installs: 0
Starts: 0
Stops: 0
Uninstalls: 0
Blockers: 0
The submitted load was refused. Review these current effects, then start a new load with the current plan digest.
Error: POST /api/profile/1/load profile.stale_plan HTTP 409: Fleet profile preview is stale; review the profile again before loading request_id=00153ff8-e9fa-4e52-b621-99e2c89e2f07 [exit]
Code: profile.stale_plan
Detail: Fleet profile preview is stale; review the profile again before loading
Operation: POST /api/profile/1/load
Endpoint: /api/profile/1/load
HTTP status: 409
Request ID: 00153ff8-e9fa-4e52-b621-99e2c89e2f07
Source: remote_rejection
Decision: exit
Request key: 11111111-1111-4111-8111-111111111111
Next: vonkctl --profile 1 profile load --dry-run
```

### Cancellation remains visible in Activity

Capture record `6`; exit `0`.

```sh
vonkctl fleet activity --state cancelling --request-id 00000000-0000-4000-8000-0000000003d4
```

stdout:

```text
Activity: 1 of 1 references on this page
Operation: a73fd65e-ae77-43c5-a68d-f85aa2813d8d
Kind: fleet-profile.apply
State: cancelling
Created: 2026-08-07 12:00:00+00:00
Targets: spk_00000000000000000000000000000001
Attempt: 1
Owner: fleet-profile-application a73fd65e-ae77-43c5-a68d-f85aa2813d8d
Request: 00000000-0000-4000-8000-0000000003d4
Reconnect: vonkctl profile progress --application a73fd65e-ae77-43c5-a68d-f85aa2813d8d --follow
Cancellation: cancelling (operator)
Cancellation request: 9ebb82e1-7d73-4828-b673-87eed72ffbb6
Cancellation actor: administrator
Completed effect count: 0
Pending effect count: 0
Cancelled or unissued effect count: 1
Cancelled or unissued effect: not-issued: profile-step step:0: Not issued profile step 1: Switch profile Cancellation boundary
Next: inspect

More results: no
```

stderr:

```text
Reason: Cancellation requested; reconciling issued profile effects.
```

### Definitive artifact refusal offers inspection

Capture record `15`; exit `2`.

```sh
vonkctl recipe job submit cbe80d77-f606-4945-a2cd-86cdeb2b04fa --request-key 00000000-0000-4000-8000-000000000105
```

stderr:

```text
Request key: 00000000-0000-4000-8000-000000000105
Retry: vonkctl recipe job submit cbe80d77-f606-4945-a2cd-86cdeb2b04fa --request-key 00000000-0000-4000-8000-000000000105
Error: POST /api/artifact-jobs/cbe80d77-f606-4945-a2cd-86cdeb2b04fa/submit http.409 HTTP 409: artifact job was submitted under another request identity [exit]
Code: http.409
Detail: artifact job was submitted under another request identity
Operation: POST /api/artifact-jobs/cbe80d77-f606-4945-a2cd-86cdeb2b04fa/submit
Endpoint: /api/artifact-jobs/cbe80d77-f606-4945-a2cd-86cdeb2b04fa/submit
HTTP status: 409
Source: remote_rejection
Decision: exit
Request key: 00000000-0000-4000-8000-000000000105
Next: vonkctl recipe job detail cbe80d77-f606-4945-a2cd-86cdeb2b04fa
```

### Previously verified local result reused

Capture record `18`; exit `0`.

```sh
vonkctl recipe job download cbe80d77-f606-4945-a2cd-86cdeb2b04fa --output /disposable-fixture/downloaded-output
```

stdout:

```text
Artifact job: cbe80d77-f606-4945-a2cd-86cdeb2b04fa
State: succeeded
Output manifest SHA-256: a1a274c053e9d995e8d2423af969910f0ebcd3b6e2f0ac093fc21ac9953eefec
Total output bytes: 30 B
Verified output files: 1
Output file: output.png
File state: reused
Verified path: /disposable-fixture/downloaded-output/output.png
Verified size: 30 B
Verified SHA-256: ea766b651151a3c7fa7638aa0cbad3fcd54e627daf1cdeac96f99702eb50a154
```

### Successful empty download

Capture record `25`; exit `0`.

```sh
vonkctl recipe job download 80bf7712-d97f-4ed5-ab89-adb8a8a7c172 --output /disposable-fixture/empty-result-output
```

stdout:

```text
Artifact job: 80bf7712-d97f-4ed5-ab89-adb8a8a7c172
State: succeeded
Output manifest SHA-256: 1ebc44849dc2e50399317bcb0d24c20fec29d28586074cfc39fd363cf50f4556
Total output bytes: 0 B
Result files: none (job succeeded with an empty result).
```

### Required output missing

Capture record `31`; exit `0`.

```sh
vonkctl recipe job detail f6527ad2-2e63-47f4-9991-e1aa48dd3019
```

stdout:

```text
Artifact job: f6527ad2-2e63-47f4-9991-e1aa48dd3019
Command: detail
Run: 9180201e-42da-45dd-be6d-9dd21099a450
State: failed
Interface: image-job
Contract SHA-256: c5da774a5943cc127067f1e4be56782dced94515991fb1c4554f67f3dee0d277
Input manifest SHA-256: 535a205632af975fdb14de354ee37342c68da52ae700993e7494d6e9c668d5db
Input bytes: 20 B
Created: 2026-08-07 12:00:00+00:00
Updated: 2026-08-07 12:00:00+00:00
Reason: artifact output slot image file count is invalid
Operation: c987c828-9e94-4033-9f4f-ac6bdd017f88
Submit request: 00000000-0000-4000-8000-000000000107
Inputs: 1 declared, 1 uploaded
Input: input.png
Input slot: input
Input state: uploaded
Input media type: image/png
Input size: 20 B
Input SHA-256: 9623dade440176b2fbd783abef030a9dea7b058cbb1fbf2c758bdddd61ca54d2
Result files: unavailable until job succeeds (state: failed)
```

Reproduce with the [capture procedure](../../plans/cli-output-capture.md).
The independent [walkthrough scorecard](../../plans/cli-operator-walkthrough.md)
remains separate and uncompleted.
