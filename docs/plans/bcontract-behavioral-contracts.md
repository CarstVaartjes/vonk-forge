# Behavioral contracts: bcontract review evidence

This change concerns source and local execution evidence. It does not claim
publication, Controller deployment, or physical Spark qualification.

## Checklist evidence

| Requirement | Producer and consumer | Regression or recovery proof |
| --- | --- | --- |
| Two closed outcome families | `agent_protocol/src/vonk_agent_protocol/http_failure.py`; generated Rust wire, TS, Python client and vocabulary; `control/src/vonk_control/http_errors.py`; `api/common.py:281`; `api/application.py:344` | `control/tests/test_http_failure_contract.py:165` rejects bare security producers and checks outer serialization; generated-contract and registry guards |
| One bounded retry owner | `rust/crates/vonk-agent/src/client/errors.rs:114`; `client/response.rs`; `pair.rs`; `executor/preflight.rs:154`; `src/cluster_profiles/control_client/common.py`; `control_client/client.py` | response, pairing, transport, heartbeat and CLI deadline tests check typed refusal, malformed peer answers, minimum delay and bounded completion |
| Renewal reconciles | `certificate_rotation_loop.rs:118`; `identity.rs:465`; `identity.rs:1003`; `rotation.rs:99`; carry3's existing Controller rotation reconciliation remains the issuance owner | provider states in `test_http_failure_contract.py`; `identity_renewal_tests.rs`; actual managed CA/PostgreSQL outages and dual-restart tests |
| Renewal alerts | systemd status in `certificate_rotation_loop.rs:133`; monitor collection in `telemetry.rs`; canonical telemetry fields; Controller projection and `metrics.py` | connected telemetry/API/SQL/projection/metrics test in `test_http_failure_contract.py:109` |
| Crash-only recovery | existing noexit service units retain `Restart=always` and disabled start limits; main observes fresh credential content rather than repeating a denied identity | `tests/nodes/test_agent_boot_restart_systemd.py`: seven failed starts, eighth recovers without reset; durable CSR, transfer and receipt tests |
| Provider-state contracts | renewal, enrollment, profile load, stop and distribution states in `test_http_failure_contract.py` | every recovery case clears the injected fault and admits fresh work |
| Real fault injection | `test_fault_recovery_postgres.py`; `test_ca_image_controller_postgres.py`; agent distribution recovery tests; `scripts/tests/run-disk-full-recovery` | real CA down/up, Controller/process death and PostgreSQL commit faults; isolated 2 MiB full filesystem ends without publication and fresh transfer succeeds |

The six enrollment/CA files from the committed faults track were integrated
without its independent workflow edits. Newer grants fence obsolete publishers;
replay preserves accepted request identity and the original expiry. The existing
carry3 renewal producer is reused, rather than duplicated in a client.

Two nullable telemetry columns are added. Startup's existing transactional schema
reconciliation owns their installation. This requires an explicit schema merge
decision; no database reset or deployment is included.

## Principle self-check

Locations below refer to the current working tree. Python server paths are
relative to `control/src/vonk_control/`; client Python paths are relative to
`src/cluster_profiles/`; agent Rust paths are relative to
`rust/crates/vonk-agent/src/`. Helper Rust paths name their crate explicitly.
Each row has exactly one
verdict. Repeated line numbers in a row are distinct branches with the same
behavior. Local state is never classified as caller input or a security edge.
Generated validation delegates to these owning paths; test assertions fail the
verification process and do not create product refusals or retained claims.

| Error, return, refusal or wait | Verdict | Behavior and owner |
| --- | --- | --- |
| `http_errors.py:13,19,36`; `auth.py:302` | SECURITY EDGE | Missing/denied authenticated authority; closed refusal header. Fresh authenticated requests are independent. |
| `agent_api/artifacts.py:236` | SECURITY EDGE | Uploaded bytes fail the bound ingress digest; partial checkpoint is truncated and never published. |
| `agent_api/artifacts.py:310,368`; `agent_api/authority.py:96,376,416,446`; `agent_api/enrollment.py:150`; `agent_api/observations.py:494` | SECURITY EDGE | Signed identity or request lacks authority for the effect. |
| `agent_api/authority.py:341` | SECURITY EDGE | Expired credential proof is outside authorized recovery grace. Re-enrollment supplies fresh authority. |
| `agent_api/common.py:376,425,438,462`; `api/application.py:470,478,487,493,501,653` | SECURITY EDGE | Required authenticated identity/source/session is absent or inactive. |
| `agent_api/common.py:448`; `api/application.py:510,521` | SECURITY EDGE | Authenticated identity or operator authority cannot authorize this effect. |
| `artifact_job_api.py:69,371,414`; `auth_api.py:96,112,122,143,156`; `catalog_api.py:148` | SECURITY EDGE | Missing authentication or denied operator role. |
| `fleet_profile_api.py:71,237,299`; `gateway_keys.py:981`; `installation_reconciliation_api.py:59`; `model_cache_api.py:130` | SECURITY EDGE | Missing administrative authority for the requested mutation. |
| `operator_projection_api/errors.py:131`; `operator_projection_api/routes.py:135`; `profile_application_cancel_api.py:52,66,97,112`; `recipe_image_availability_api.py:356,407` | SECURITY EDGE | Denied authenticated authority, including cancellation and artifact effects. |
| `api/common.py:281`; `api/application.py:344,438` | HEALS | Unknown non-success/exception and unreadable outcome header become bounded typed transient; retry hint survives; a subsequent request has no retained gate. |
| `agent_api/authority.py:86,103,332,372,412,442` | HEALS | Dependency/local credential observation retries within its owner budget, then returns transient; renewal keeps its durable CSR and fresh requests remain admissible. |
| `agent_api/common.py:363,369,503,508,565,575,583,607,610,618,641,645,665,668,694,696,704,708,714,718,736` | HEALS | Unavailable service, cache, assignment, checkpoint or file is a bounded unknown/miss. Upload isolates damaged entries; reads re-observe, transfer retries or ends, and no lock or false publication survives. |
| `agent_api/common.py:753,755,759,761` | CALLER REQUEST INVALID | Malformed caller byte range, rejected before reading or publishing an effect. |
| `agent_api/artifacts.py:69,76,190,196,200,221` | CALLER REQUEST INVALID | Caller size, offset, framing or assignment request is malformed before the corresponding write/read effect. |
| `agent_api/artifacts.py:84,137,153,213,228,260,268,272,333,363,390`; `345,380` non-authority branches | HEALS | Lease/storage or changed checkpoint is re-observed; checkpoint remains resumable or isolated, writer lock is released, and a fresh transfer is admitted. |
| `agent_api/enrollment.py:55,60,71,96` | HEALS | Bootstrap/authority availability and rate limits emit transient; the next bounded enrollment/bootstrap observation uses current state. |
| `agent_api/enrollment.py:112,119,123,127,131,137,156` | CALLER REQUEST INVALID | Malformed external enrollment token/evidence/CSR or canonical byte budget before issuance. Actual authority denial uses the separate security branch. |
| `agent_api/authority.py:322,366,406` | CALLER REQUEST INVALID | Caller expired proof shape or non-ASCII CSR before issuance. |
| `enrollment/issuance.py:113,206,250,252`; `enrollment/service.py:323` | CALLER REQUEST INVALID | Caller CSR/evidence/request identity/activation input is malformed before the corresponding effect. |
| `enrollment/issuance.py:168,271,277,333,341,358,392,402,406,487,535,541,558,584,592,639,647,659,687` | HEALS | Durable issuance/replay owner retries bounded SQL/CA observation, preserves original deadline, fences obsolete publishers, and admits newer grants without waiting behind them. Missing records become observation outcomes at the API boundary. |
| `enrollment/service.py:304,467,476,500,515,531,560,577,598,617,635` | HEALS | Bounded revocation/rotation/activation observation; exact effect reconciliation and current intent survive process death; later requests do not inherit an exhausted observation budget. |
| `client/errors.rs:8,10,12,16,18`; `client/response.rs:270,283` | HEALS | Credential read, local TLS material, transport, unparseable peer response and typed unknown use the sole bounded caller retry owner. |
| `client/errors.rs:14,130,142`; `client/transport.rs:38`; `agent_api/artifacts.py:345,380` authority-denial branches; `main.rs:480` identity-refusal branch | SECURITY EDGE | Only the authority's closed refusal family and verified ingress integrity pause/refuse action; identity waits for fresh authority, content refuses only its operation. |
| `client/errors.rs:27,34` | HEALS | Superseded result retains unacknowledged evidence and reconnects/reconciles; rejected result records a bounded ending and permits new operations. |
| `client/errors.rs:155`; `client/jobs.rs:358` | HEALS | Full jitter capped at 60 seconds plus the server minimum; upload owner's attempt/deadline bounds terminate observation and preserve resume state. |
| `client/jobs.rs:17,27,49,65,105,142,154,179,190,248,260,279,285,309,335` | HEALS | Malformed peer receipts and damaged local upload state are unknowns; HTTP calls have timeouts, partial work is reusable, and transfer owner retries or ends without publication. |
| `client/storage.rs:31,36,52,64,109,142,178,234` | HEALS | Local file/journal/checkpoint faults are misses/unknowns; managed transfer repairs or re-fetches within its deadline, then ends and allows fresh transfer. |
| `client/transport.rs:11,46,61,108,109,137,146,150,169,180,191,198`; `client/renewal.rs:45,46,63,71,82,92` | HEALS | Bounded client construction/transport lock/body/HTTP observation; local CA pin damage is an unknown. A typed security result is forwarded unchanged, never reclassified by status. |
| `client/distribution.rs:328` | SECURITY EDGE | Peer substitution violates the expected ingress content digest; no unverified final object is published. |
| `client/distribution.rs:17,40,117,247,251,339,346,362,394,419,432,455` | HEALS | Protocol/local storage/partial-body uncertainty retries under the single transfer owner or ends boundedly; partial bytes are retained safely and fresh work is admitted. |
| `pair.rs:133,153,348` | SECURITY EDGE | Authority's typed refusal, or issued node/key/digest mismatch at ingress, prevents accepting unverified credentials. |
| `pair.rs:68,75,184,196,222,226,236,244,247,274,300` | HEALS | Local CA/evidence and malformed/oversized peer answers are unknowns, not identity refusal. Pairing ends within four attempts/300 seconds; the same durable CSR and a fresh invocation remain available. |
| `pair.rs:90,91` and pairing timeout-at/backoff waits | HEALS | One owner, 30-second calls and 300-second total budget; server minimum precedes capped jitter. |
| `pair.rs:292`; `config.rs:70`; `main.rs:192` | CALLER REQUEST INVALID | Malformed caller token, explicit renewal configuration outside its allowed range, or unsafe token argument before enrollment effects. |
| `rotation.rs:149`; `identity.rs:70,91,206,218,245,261,273,281,284,291,294,393,407,418,440,473,550,563,573,578,590,619,638,650` | HEALS | Local active/staged/pending identity storage is re-observed by the 300-second renewal owner; damaged metadata is a miss and exact publication remains fenced. No local record rejects authority. |
| `identity.rs:1003,1013` | HEALS | Health write/read damage cannot block credential use or admission; the monitor omits unknown evidence and the next observation repairs/replaces it. |
| `certificate_rotation_loop.rs:127,163`; its observation-ended returns | HEALS | Sole production renewal owner retries the durable CSR; each call ends in 300 seconds, each next attempt has bounded RetryInfo/jitter, and expiry never terminates reporting. Test-only settling helper ends after four observations. |
| `certificate_rotation_loop.rs:135`; `main.rs:285,297,321` | SECURITY EDGE | Verified identity refusal stops remote action; the running daemon observes new certificate content with bounded local polls, then admits fresh authority without blindly repeating the refused request. |
| `main.rs:106,256,273,303,304,385,395,413,426,431,481,497,542,547,549,578,592,597,633,637,640,653` | HEALS | Session/lane, inventory, local checkpoint and package-ack uncertainty use bounded poll/HTTP/attempt budgets and supervision; exact authority refusal is dispatched to the distinct fresh-authority path above. |
| `executor/distribution.rs:131`; `executor/heartbeat.rs:94,134`; `executor/preflight.rs:66,143,154` | HEALS | Accepted lease/deadline bounds the wait. Helper preflight makes one deadline-bound call; Controller scheduler owns retry. Heartbeat honors RetryInfo and checks immutable expiry before another call. |
| `executor/installation.rs:36,67,69,71,76,96,99,105,109` | HEALS | Preparation ends with exact cancellation, dependency/cleanup observation or authoritative operation refusal; cleanup reconciliation retains exact uncertain effects and frees attempt state for fresh work. |
| `host_runtime/responses.rs:8,49,54,61,67`; `host_runtime/errors.rs` non-refusal variants; `agent_upgrade.rs` malformed/helper I/O variants | HEALS | Unbound/unparseable helper reply and damaged request storage are typed unknowns; operation returns temporary dependency to the Controller's bounded reconciliation scheduler. No second authority gate. |
| `vonk-agent-helper/src/failure.rs:9,19`; `vonk-agent-helper/src/main.rs:317,346` | SECURITY EDGE | Only invalid signed grant, denied grant/node authority, and unknown peer identity emit refusal. |
| `vonk-agent-helper/src/failure.rs:26`; `vonk-agent-helper/src/main.rs:317,346` | HEALS | Ledger, operation I/O, busy concurrency and other local observations emit transient with required delay/window; locks/slots release and subsequent calls can run. |
| `control_client/transport.py:57,65,68,70,79,83,90,103` | HEALS | Local token file is unavailable/unsafe; one bounded CLI attempt ends before effect, and a fresh invocation reads current authority rather than inheriting a denial. |
| `control_client/transport.py:140`; `control_client/client.py:217,507,708` transient/unknown branches | HEALS | Typed transient/unreadable answer enters the sole observation owner; finite deadline ends observation without cancelling remote work. The authority's explicit refusal is surfaced separately. |
| `control_client/transport.py:157,202,207,241,245`; `control_client/client.py:251,265,290,328,356,360,408,417,437,459,489,535,540,543,568,574,585,595,624,641,658,670,675,681,740,755,759,763` | HEALS | Timeout, oversized/unparseable response and missing schema are bounded peer observations; reconnect uses original request/operation identity, and an ended attempt does not poison fresh admission. |
| `control_client/client.py:104,109,111,297,368,385,788,790` | CALLER REQUEST INVALID | Caller URL/path, finite request/wait bounds, or explicit transfer arguments fail before any effect. |
| `control_client/client.py:796,800,804,813,820,821,824,829,831,834,836,837,838` | HEALS | Existing wait observes the operation under its explicit deadline, ends on terminal/unknown state, and does not mutate or cancel it. Fresh admission remains with the Controller, not this observer. |
| `control_client/transport.py:140`; `control_client/client.py:217,507,708` closed refusal branches; `controller_cli/submission.py` typed auth refusal branch | SECURITY EDGE | Actual denied authority is surfaced; unknown status/response reconciles request identity instead of repeating effects. |
| `scripts/tests/run-disk-full-recovery:4` | CALLER REQUEST INVALID | Incorrect test-runner arguments before mount/effect. |
| `scripts/tests/run-disk-full-recovery:5` | SECURITY EDGE | Required root authority for the explicitly disposable mount is missing. |
| `scripts/tests/run-disk-full-recovery:6,7,9,10,12,14,19` | HEALS | Missing local test/storage, mount failure and execution deadline end boundedly; owned temporary mount is unmounted/removed, next invocation creates independent storage. |

The full-filesystem test's outer 90-second process timeout and inner 15-second
transfer timeout are HEALS: they bound the injected fault, never publish partial
bytes, and require a fresh successful transfer once space is freed. Its asserts
and the provider-state asserts are verification failures, not product gates.

## Verification results

- Full portable Controller: 4,942 passed, 174 skipped.
- Full portable repository: 1,420 passed, 90 skipped, 235 Linux selections
  deselected, 45 subtests passed.
- Full Linux Controller selection: 648 passed, 8 skipped, one obsolete
  serial-admission expectation failed. Its replacement proves newer admission and
  old-publisher fencing against real PostgreSQL and passed after correction.
- Full Linux repository selection: 224 passed, 7 skipped.
- Actual managed CA/PostgreSQL fault tests: 5 passed, including simultaneous
  restart and lost committed reply.
- Four touched Rust crates: fmt, clippy, unit/integration/doc tests passed,
  including automatic isolated-full-filesystem recovery. Two pre-existing
  designated hardware cases remain ignored.
- Disposable systemd: seven failed starts, automatic recovery on eighth start,
  no manual reset and no start-limit dead end.
- Always-on guards: 341 passed. Available requested Controller static tests:
  160 passed. Repository/generated/hermeticity static tests: 68 passed.
- Changed Controller test files: 248 passed. Provider/OpenAPI tests: 11 passed.
- Ruff, formatting, baseline-aware Python types, shell syntax, added-line scans
  (including untracked source), and diff whitespace checks passed.

Measured source changes versus `origin/main`: bare literal 401/403
`HTTPException` producers 52 → 0; free-string `ControllerRefusalBody` 1 → 0;
catch-all Rust `Retryable` variant 1 → 0. These are direct source measurements,
not lifecycle debt classifications.

The prescribed lifecycle report cannot run: `scripts/lifecycle-counts` does not
exist in this checkout. Git is read-only, so the coordinator owns the commit and
any post-commit report. The prescribed static command also names three absent
files: `test_blocker_boundaries.py`, `test_blocker_classifier.py`, and
`test_blocker_retries.py`. Current principle/untyped/request-led guards and the
full suites were run instead; no substitute counts or baseline were invented.
