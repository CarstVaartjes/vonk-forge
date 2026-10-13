# Proof workflow ownership

The complete control suite collects `control/tests` through `scripts/test control`.
Its only exclusions are `built_image` and `*_wire_bridge.py`, which have their own
regular CI lanes. `lane` and `postgres` are included. The shards use the same
collection options and distribute whole files. PostgreSQL fixture consumers get
the prerequisite marker from `control/tests/conftest.py`; CI pulls the reviewed
PostgreSQL image, supplies the recipe library, and synchronizes CLI dependencies.

The retirement audit checked every pytest target in the removed workflows against
these selectors, its current test definitions and skip/prerequisite markers. It
was a source-selection audit; the suites were not executed locally.

| Workflow (`*-proof.yml`) | Verdict | Test ownership or unique boundary |
| --- | --- | --- |
| artifact-cas-retention-recreation | Redundant; removed | Complete control suite: `test_artifact_cas_retention_recreation.py`. |
| damaged-image-availability | Redundant; removed | Complete control suite: `test_damaged_image_availability_recovery.py`; CI now installs and requires skopeo. |
| exact-integer-storage | Redundant; removed | Complete control suite: `test_exact_integer_storage.py`, `test_database_adoption_recovery_postgres.py`, `test_database_connection_wait_postgres.py`; all named startup cases and both integer engines are selected. |
| observation-capture | Redundant; removed | Complete control suite: `test_observation_capture_postgres.py`, including its snapshot, slow-stream and retry cases. |
| profile-admission-contention | Redundant; removed | Complete control suite: `test_profile_load_submission.py::test_superseded_child_contention_is_parked_without_holding_admission`, both locked-model parameters. |
| runtime-cache-generation | Redundant; removed | Complete control suite: `test_runtime_cache_generation_reuse.py`; CI now installs and requires skopeo. |
| selected-profile-author-continuity | Redundant; removed | Complete control suite: `test_selected_profile_authority_continuity_postgres.py`, both disabled/demoted parameters. |
| startup-bookkeeping-recovery | Redundant; removed | Complete control suite: `test_startup_retained_repair_postgres.py`; removed its sole-use `scripts/prove-startup-bookkeeping-counterexample`. |
| installed-cli-transition | Unique; retained | Actual frozen prior CLI to current Controller transition, signed bootstrap and offline uv tool install; ordinary repository collection skips this opt-in lane. |
| installed-cli-update | Unique; retained | Actual signed-wheel replacement and tamper refusal with offline uv tool installation; ordinary repository collection skips this opt-in lane. |
| model-cache-unknown-expiry | Unique; retained | The browser consumes responses from the real PostgreSQL/API cancellation journey. Ordinary control and web suites run separately and do not supply this handoff. |
| profile-effect-consumer | Unique; retained | Real SQL Stop through an installed CLI and the immutable sibling recipes cleanup consumer. Ordinary control collection skips without that consumer checkout. |

Unique lanes retain test outcome assertions and security checks on actual inputs.
They no longer record source/wheel provenance manifests, attest hosted outputs,
or upload handoff documents solely for auditing. Logs and JUnit results remain.
The prior CLI and recipes commit pins are immutable test inputs, not test-count
pins. Source build identities still exercise the installed contract.

`tests/test_workflow_assertions.py` caps proof workflow/job inventory at five
(lower both ceilings when another lane retires), rejects proof provenance
recording, preserves the no-pinned-case-count guard, and checks that regular
control CI includes lanes and requires its OCI ingress prerequisite. The shared
prerequisite policy fails CI when skopeo is missing instead of silently skipping.
Every retained Python/Go/browser lane requires nonempty successful execution,
no skips/failures, and named boundary cases.

## Required checks

Searching `.github` and existing docs found no references to the removed job
display names. Remote branch-protection settings were not inspected or changed.
If any are required checks in GitHub, the owner must remove these contexts and
retain the regular CI aggregate gate:

- Reference-safe CAS collection through restarted owner and fresh claim
- Real damaged image availability recovery
- Native SQLite and PostgreSQL exact integer storage and adoption
- SQL capture, slow-stream release and same-read repair
- Locked parent and child park without holding admission
- Real OCI ingress and exact plan reuse
- Issued Stop survives revocation and same selection resumes cleanup
- Actual adoption SQL fault and same-intent recovery
- Native transport activation timeout and same-generation recovery

Inventory: 13 → 5 proof workflows; 14 → 5 proof jobs; 1 → 0 `scripts/prove-*`
helpers. No test functions were removed.

## 2026-10 retirement: duplicated pull-request lanes

The same source-selection audit, against the regular CI jobs of `ci.yml` and
the pre-merge candidate acceptance (`release-acceptance.yml`):

| Workflow | Verdict | Test ownership or unique boundary |
| --- | --- | --- |
| consumer-contract-corpus | Redundant; removed | `Build actual native consumer probe` builds the same probe; the control shards run `test_consumer_corpus_boundaries.py` against it; `Rust workspace tests` runs `pair::tests` and `raw_integer_shape_tests`. The uploaded corpus had no consumer. |
| contract-artifacts | Redundant; removed | `Rust workspace tests` checks wire generation determinism from the same source; the uploaded artifacts had no consumer. |
| agent-upgrade-recovery | Redundant; removed | `Native ARM64 agent package lifecycle` already ran `test_agent_upgrade_recovery_systemd.sh` in every crash mode; its two unique proofs, `test_agent_package_rollback_systemd.py` and `test_agent_boot_restart_systemd.py`, now run in that job. |
| browser-contract-artifacts | Folded into `Admin web behavior` | The Python-emitted corpus, observation transfers, official JSON Schema cases and the contracts Playwright project now run in the regular web job; the artifact uploads had no consumer. |
| installer-pty-recovery | Redundant; removed | `Build and test native Spark setup (linux-arm64)` in the pre-merge candidate acceptance runs `run_installer_pty_recovery.sh` for every change that selects it. |
| installed-cli-transition-proof, installed-cli-update-proof | Merged into `installed-cli-proof` | Both unique lanes share one checkout, environment and isolated resolver cache; the two named cases run in one invocation. |

Retained standalone lanes: `agent-repair-recovery` (full repair matrix on
disposable ARM64 systemd), `oci-link-fallback` (a bind-mounted cross-device
store), `privileged-helper-oci-process` (the signed helper on ARM64 systemd),
`model-cache-unknown-expiry-proof` and `profile-effect-consumer-proof`.
