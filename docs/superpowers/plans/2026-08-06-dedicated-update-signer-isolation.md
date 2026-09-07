# Dedicated Update Signer Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate GPU node update signing in a networkless process and atomically publish only receipts backed by durable, revalidated authorization intents.

**Architecture:** A typed Unix-socket client/server separates the worker from the private authority. The orchestrator reserves an immutable database intent, calls the signer outside transactions, then performs an exact CAS transaction that queues the operation or marks the intent stale.

**Tech Stack:** Python 3.12, SQLAlchemy/Alembic, Unix sockets with `SO_PEERCRED`, python-tuf, Ed25519, Docker Compose, pytest.

## Global Constraints

- The signer has no database, route, or network access.
- The worker receives no authority key or bootstrap root.
- IPC is canonical ASCII JSON, newline terminated, and at most 64 KiB.
- Signer peer UID is exactly 10001; signer UID is 10003.
- Signer active-control identity comes only from immutable local configuration plus a fresh dirfd/`O_NOFOLLOW` read of `active.json` in the read-only identity-directory mount, `ActiveControlReleaseLoader`, and the exact versioned verified TUF target; an init copy or candidate projection is never accepted.
- Every request carries a distinct API-issued Ed25519 admin grant; the signer has only its public verification key, and the worker has neither private key.
- Grants bind action/rollout/job/node set/release/nonce/expiry, last at most one hour, and bound every receipt expiry.
- Update requests bind release digest, exact TUF target SHA-256, and targets metadata version.
- No database lock or transaction spans signer IPC.
- No commit is created by this task.

---

### Task 1: Typed signer IPC and independent signer policy

**Files:**
- Create: `control/src/vonk_control/update_signer.py`
- Create: `control/tests/test_update_signer.py`
- Modify: `control/src/vonk_control/update_authority.py`

**Interfaces:**
- Produces `UnixUpdateSignerClient.authorize(request) -> dict[str, object]`.
- Produces `UpdateSignerServer.serve_once()` and a module entry point.
- Consumes the existing `UpdateAuthorizationAuthority` and `ActiveControlReleaseLoader`.

- [x] Write failing tests for canonical bounds, exact fields, peer UID, deterministic update/rollback responses, TUF mismatch, active-control mismatch, and API-grant verification.
- [x] Run only `control/tests/test_update_signer.py` and confirm the missing boundary failures.
- [x] Implement the minimal client/server parser, grant verifier, signer policy, and entry point.
- [x] Run the signer tests and authority tests to green.

### Task 2: Durable authorization intent schema

**Files:**
- Modify: `control/src/vonk_control/models.py`
- Modify: `control/migrations/versions/0011_update_rollouts.py`
- Modify: `control/tests/test_update_rollout_migration.py`

**Interfaces:**
- Produces `UpdateAuthorizationIntent` with immutable bindings and `reserved|signed|queued|stale` state.

- [x] Write failing upgrade/downgrade, constraints, and existing-row tests.
- [x] Run the single migration test and confirm the table is absent.
- [x] Add the model and reversible table migration, including exact grant and TUF bindings.
- [x] Run migration/model tests to green.

### Task 3: A/B/C queue publication and recovery

**Files:**
- Modify: `control/src/vonk_control/agent_jobs.py`
- Modify: `control/src/vonk_control/updates.py`
- Modify: `control/tests/test_agent_jobs.py`
- Modify: `control/tests/test_update_orchestrator.py`

**Interfaces:**
- Produces reservation, signer-call, and CAS-finalization methods used by both update dispatch and operator rollback.
- Consumes `UnixUpdateSignerClient.authorize`.

- [x] Write failing update and rollback tests for A/B/C boundaries, exact CAS/source drift, arbitrary response rejection, no transaction during IPC, and PostgreSQL claim/failure serialization.
- [x] Run the named tests and confirm current direct signing/transaction behavior fails.
- [x] Implement intent reservation and finalization in `AgentJobService`.
- [x] Refactor update dispatch and rollback to call the signer between short transactions.
- [x] Quarantine invalid rollback candidates and continue selecting eligible operations.
- [x] Run agent-job and update-orchestrator tests to green.

### Task 4: Production settings and Compose separation

**Files:**
- Modify: `control/src/vonk_control/settings.py`
- Modify: `control/src/vonk_control/worker.py`
- Modify: `control/tests/test_settings.py`
- Modify: `control/tests/test_production_worker.py`
- Modify: `deploy/compose/compose.yaml`
- Modify: `deploy/compose/.env.example`
- Modify: `deploy/compose/tests/test.env`
- Modify: `deploy/compose/tests/test_networking.py`
- Modify: `deploy/compose/README.md`

**Interfaces:**
- Worker settings expose only the signer socket path.
- Signer settings own key/bootstrap/publication/cache paths and the read-only control-identity directory path.

- [x] Write failing settings and Compose tests proving UID, socket, network, secret, mount, and cache separation.
- [x] Run the focused settings/Compose tests and confirm worker still owns secrets.
- [x] Add signer service/settings, safe active-generation projection, and replace direct worker operation with the Unix client.
- [x] Run production-worker, settings, networking, and documentation contract tests.

### Task 5: Focused integration verification

**Files:** all files above.

- [x] Run signer, agent-jobs, orchestrator, migration, production-worker, settings, Compose networking, and focused PostgreSQL race suites.
- [x] Run Ruff on changed Python files, `python3 -m py_compile` on entry points, and `git diff --check`.
- [x] Report exact passing counts and any remaining design blocker without committing.

#### Verification checkpoint — 2026-08-06

- The focused signer/authority, agent-job, update-orchestrator, rollout
  migration, production-worker, settings, update-worker, generation-readiness,
  and Compose networking selection passed **259 tests in 41.51s**.
- The PostgreSQL agent-job and reconciliation race selection passed **64 tests
  in 29.92s**.
- The broader host-generation/release selection passed **102 tests in 7.97s**;
  the control host boundary selection passed **275 tests in 29.91s**; the
  agent update selection passed **21 tests in 0.20s**; and Compose networking
  passed **12 tests in 1.37s**.
- Ruff `0.16.1`, Python entry-point compilation, and `git diff --check` passed.
- No code-level blocker remains for signer isolation. The first real release
  still requires external physical control-host update/recovery, GPU node
  canary/rollback, replacement-host, authenticated-encryption, and signed
  platform-manifest evidence; this checkpoint does not fabricate those gates.
