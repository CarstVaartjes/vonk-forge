# Actual installed CLI examples

Source checkpoint: `134bcbf9fd54a70da2be8204a805dfd5c34feccc`.
Captured: `2026-09-24T14:20:36.079741+00:00`. Seven installed acceptance tests passed; the
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
Plan digest: d88f0400e3293d571911301b2a57a860907773bb0fdea5db2a5736f304b49a2c
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
Run: d1ee5245-7c68-4bd1-9b7c-ec92c400ed2d
Complete group: spk_00000000000000000000000000000001, spk_00000000000000000000000000000002
Keep installation: 4a8ca52d-3d82-44f8-af0d-849e02705be7
Complete group: spk_00000000000000000000000000000001, spk_00000000000000000000000000000002
Assignment: 756cf4c0-cce5-5c9f-858a-0edd415ead81
Exact model set: ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
Exact image: sha256:1111111111111111111111111111111111111111111111111111111111111111
OCI archive SHA-256: fc3674abb069b349caf8573256160eb1be4ccc4851e69b14873662f3f189e191
Image size: 30 B
Architecture: linux-arm64
Runtime interface: vonk.runtime.v1
Build: 511674d8-1bd9-4ab3-a7ee-2f8e420c89d9
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
Memory after placement: unavailable
Resident usage evidence: Per-run usage is unavailable; admission uses each full residual upper bound.
Capacity source: aggregate_inventory_without_run_usage
Inventory sample: 2026-08-07T12:00:00Z
Inventory evidence digest: a5544eee276c4442c13a58c0b304f4df7db7bbd3dd863531e9d5678794cb8c77
Possible run residual: d1ee5245-7c68-4bd1-9b7c-ec92c400ed2d generation 1: 0–225 B (not measured)
Disk required: 1.1 KiB (1084 bytes)
Disk after placement: 5.7 KiB (5872 bytes)
Attention: spk_00000000000000000000000000000001 capacity blocker: run.port_occupied: Port 8000 is already reserved on this GPU node.
Attention: spk_00000000000000000000000000000001 capacity blocker: run.rendezvous_port_occupied: Multi-node rendezvous port 29500 is already reserved.
Attention: spk_00000000000000000000000000000001 capacity warning: run-switch.resource.estimate_uncertain: Forecast 225 bytes from the declared recipe-role memory envelope (225 bytes); runtime_overhead_bytes is unavailable, so actual demand may exceed this bound.
Attention: spk_00000000000000000000000000000001 capacity warning: run-switch.resource.resident_usage_unknown: Aggregate inventory 2026-08-07T12:00:00+00:00 (a5544eee276c4442c13a58c0b304f4df7db7bbd3dd863531e9d5678794cb8c77) reports no per-run resident usage for 1 exact active claim(s); their remaining commitment is in the range 0..225 bytes. Admission applies the full upper bound.
Spark: spk_00000000000000000000000000000002
Ports required: 8000
Memory demand kind: unified
Physical memory pool: shared
Memory required: 225 B
Memory reserve: 50 B
Physical memory capacity: 9.8 KiB (10000 bytes)
Available in limiting pool: 7.8 KiB (8000 bytes)
Memory after placement: unavailable
Resident usage evidence: Per-run usage is unavailable; admission uses each full residual upper bound.
Capacity source: aggregate_inventory_without_run_usage
Inventory sample: 2026-08-07T12:00:00Z
Inventory evidence digest: bb481bff33c1cbed852aef9d42c5535793ececbc8fd8aba259ceefc72738094d
Possible run residual: d1ee5245-7c68-4bd1-9b7c-ec92c400ed2d generation 1: 0–225 B (not measured)
Disk required: 1.1 KiB (1084 bytes)
Disk after placement: 5.7 KiB (5872 bytes)
Attention: spk_00000000000000000000000000000002 capacity blocker: run.port_occupied: Port 8000 is already reserved on this GPU node.
Attention: spk_00000000000000000000000000000002 capacity warning: run-switch.resource.estimate_uncertain: Forecast 225 bytes from the declared recipe-role memory envelope (225 bytes); runtime_overhead_bytes is unavailable, so actual demand may exceed this bound.
Attention: spk_00000000000000000000000000000002 capacity warning: run-switch.resource.resident_usage_unknown: Aggregate inventory 2026-08-07T12:00:00+00:00 (bb481bff33c1cbed852aef9d42c5535793ececbc8fd8aba259ceefc72738094d) reports no per-run resident usage for 1 exact active claim(s); their remaining commitment is in the range 0..225 bytes. Admission applies the full upper bound.
Capacity after stops: fits
Spark: spk_00000000000000000000000000000001
Ports required: 8000, 29500
Memory demand kind: unified
Physical memory pool: shared
Memory required: 225 B
Memory reserve: 50 B
Physical memory capacity: 9.8 KiB (10000 bytes)
Available in limiting pool: 7.8 KiB (8000 bytes)
Memory after placement: 7.6 KiB (7775 bytes)
Disk required: 1.1 KiB (1084 bytes)
Disk after placement: 5.7 KiB (5872 bytes)
Attention: spk_00000000000000000000000000000001 capacity warning: run-switch.resource.estimate_uncertain: Forecast 225 bytes from the declared recipe-role memory envelope (225 bytes); runtime_overhead_bytes is unavailable, so actual demand may exceed this bound.
Spark: spk_00000000000000000000000000000002
Ports required: 8000
Memory demand kind: unified
Physical memory pool: shared
Memory required: 225 B
Memory reserve: 50 B
Physical memory capacity: 9.8 KiB (10000 bytes)
Available in limiting pool: 7.8 KiB (8000 bytes)
Memory after placement: 7.6 KiB (7775 bytes)
Disk required: 1.1 KiB (1084 bytes)
Disk after placement: 5.7 KiB (5872 bytes)
Attention: spk_00000000000000000000000000000002 capacity warning: run-switch.resource.estimate_uncertain: Forecast 225 bytes from the declared recipe-role memory envelope (225 bytes); runtime_overhead_bytes is unavailable, so actual demand may exceed this bound.
Attention: run-switch.resource.estimate_uncertain: Forecast 225 bytes from the declared recipe-role memory envelope (225 bytes); runtime_overhead_bytes is unavailable, so actual demand may exceed this bound.
Attention: run-switch.resource.resident_usage_unknown: Aggregate inventory 2026-08-07T12:00:00+00:00 (a5544eee276c4442c13a58c0b304f4df7db7bbd3dd863531e9d5678794cb8c77) reports no per-run resident usage for 1 exact active claim(s); their remaining commitment is in the range 0..225 bytes. Admission applies the full upper bound.
Attention: run-switch.resource.estimate_uncertain: Forecast 225 bytes from the declared recipe-role memory envelope (225 bytes); runtime_overhead_bytes is unavailable, so actual demand may exceed this bound.
Attention: run-switch.resource.resident_usage_unknown: Aggregate inventory 2026-08-07T12:00:00+00:00 (bb481bff33c1cbed852aef9d42c5535793ececbc8fd8aba259ceefc72738094d) reports no per-run resident usage for 1 exact active claim(s); their remaining commitment is in the range 0..225 bytes. Admission applies the full upper bound.
Attention: resource.estimate_uncertain: Forecast 225 bytes from the declared recipe-role memory envelope (225 bytes); runtime_overhead_bytes is unavailable, so actual demand may exceed this bound.
Attention: resource.estimate_uncertain: Forecast 225 bytes from the declared recipe-role memory envelope (225 bytes); runtime_overhead_bytes is unavailable, so actual demand may exceed this bound.
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
vonkctl --profile 1 profile load --expected-plan 1b0d557ca344249bf36fd29a9747627ea9939962bb8b48d0140b801aa9376163 --request-key 11111111-1111-4111-8111-111111111111 --detach
```

terminal:

```text
Ready for review
Profile: Reviewed idle
Revision: 1
Plan digest: 1b0d557ca344249bf36fd29a9747627ea9939962bb8b48d0140b801aa9376163
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
Load profile 1 with reviewed plan 1b0d557ca344249bf36fd29a9747627ea9939962bb8b48d0140b801aa9376163? [y/N] yes
Request key: 11111111-1111-4111-8111-111111111111
Reconnect: vonkctl --profile 1 profile progress --request-key 11111111-1111-4111-8111-111111111111 --follow
Ready for review
Profile: Edited after review
Revision: 2
Plan digest: 920377f2779a469be93bd863c0e70b582d42b37845b9d17a1b8fae6cf98a485c
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
Error: POST /api/profile/1/load profile.stale_plan HTTP 409: Fleet profile preview is stale; review the profile again before loading request_id=bfd2140b-16b8-49ec-9d6a-df9d72b67110 [exit]
Code: profile.stale_plan
Detail: Fleet profile preview is stale; review the profile again before loading
Operation: POST /api/profile/1/load
Endpoint: /api/profile/1/load
HTTP status: 409
Request ID: bfd2140b-16b8-49ec-9d6a-df9d72b67110
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
Operation: 3ab5fca8-c070-40d8-a5bf-f87666d2dbb8
Kind: fleet-profile.apply
State: cancelling
Created: 2026-08-07 12:00:00+00:00
Targets: spk_00000000000000000000000000000001
Attempt: 1
Owner: fleet-profile-application 3ab5fca8-c070-40d8-a5bf-f87666d2dbb8
Request: 00000000-0000-4000-8000-0000000003d4
Reconnect: vonkctl profile progress --application 3ab5fca8-c070-40d8-a5bf-f87666d2dbb8 --follow
Cancellation: cancelling (operator)
Cancellation request: eed77706-bba3-4edb-b6c2-790d4b478057
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
vonkctl recipe job submit 1eb7cb50-b91b-4410-be70-089825a97d45 --request-key 00000000-0000-4000-8000-000000000105
```

stderr:

```text
Request key: 00000000-0000-4000-8000-000000000105
Retry: vonkctl recipe job submit 1eb7cb50-b91b-4410-be70-089825a97d45 --request-key 00000000-0000-4000-8000-000000000105
Error: POST /api/artifact-jobs/1eb7cb50-b91b-4410-be70-089825a97d45/submit http.409 HTTP 409: artifact job was submitted under another request identity [exit]
Code: http.409
Detail: artifact job was submitted under another request identity
Operation: POST /api/artifact-jobs/1eb7cb50-b91b-4410-be70-089825a97d45/submit
Endpoint: /api/artifact-jobs/1eb7cb50-b91b-4410-be70-089825a97d45/submit
HTTP status: 409
Source: remote_rejection
Decision: exit
Request key: 00000000-0000-4000-8000-000000000105
Next: vonkctl recipe job detail 1eb7cb50-b91b-4410-be70-089825a97d45
```

### Previously verified local result reused

Capture record `18`; exit `0`.

```sh
vonkctl recipe job download 1eb7cb50-b91b-4410-be70-089825a97d45 --output /disposable-fixture/downloaded-output
```

stdout:

```text
Artifact job: 1eb7cb50-b91b-4410-be70-089825a97d45
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
vonkctl recipe job download 6647fcc3-ed11-4bc7-9e67-9031422bc28a --output /disposable-fixture/empty-result-output
```

stdout:

```text
Artifact job: 6647fcc3-ed11-4bc7-9e67-9031422bc28a
State: succeeded
Output manifest SHA-256: 1ebc44849dc2e50399317bcb0d24c20fec29d28586074cfc39fd363cf50f4556
Total output bytes: 0 B
Result files: none (job succeeded with an empty result).
```

### Required output missing

Capture record `31`; exit `0`.

```sh
vonkctl recipe job detail 8682bd9b-ee6e-45a8-b811-e8c559d6df6a
```

stdout:

```text
Artifact job: 8682bd9b-ee6e-45a8-b811-e8c559d6df6a
Command: detail
Run: 2fa97eee-02af-4ef6-a2d7-073a376aedcc
State: failed
Interface: image-job
Contract SHA-256: c5da774a5943cc127067f1e4be56782dced94515991fb1c4554f67f3dee0d277
Input manifest SHA-256: 535a205632af975fdb14de354ee37342c68da52ae700993e7494d6e9c668d5db
Input bytes: 20 B
Created: 2026-08-07 12:00:00+00:00
Updated: 2026-08-07 12:00:00+00:00
Reason: artifact output slot image file count is invalid
Operation: 6ddc5dea-12d7-4d7d-bc05-fbd1d4d61261
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
