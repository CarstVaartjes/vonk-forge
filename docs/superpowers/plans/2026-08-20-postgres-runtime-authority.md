# PostgreSQL Runtime Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the runtime Git checkout and persist control-plane authority in PostgreSQL.

**Architecture:** Add immutable database authority revisions, a persisted proposal store, and a compare-and-swap change service. Replace Git commit terminology with opaque database revision IDs throughout the runtime contracts. Remove repository/Git settings and host mounts from Compose.

**Tech Stack:** Python 3.12, SQLAlchemy, Alembic, PostgreSQL, FastAPI, Docker Compose, pytest.

**Spec:** `docs/superpowers/specs/2026-08-20-postgres-runtime-authority-design.md`

## Global Constraints

- No production runtime dependency on a host Git checkout.
- Persistent application state must be in PostgreSQL.
- Secrets and private keys remain Docker secrets or purpose-specific credential volumes.
- Database revisions must be immutable and stale proposals must fail closed.
- No migration compatibility layer is required; this is a fresh deployment schema.
- Compose must contain no repository bind mount or Git signing secret.

---

### Task 1: Add database authority and proposal persistence

**Files:**
- Modify: `control/src/vonk_control/models.py`
- Create: `control/src/vonk_control/database_authority.py`
- Modify: `control/migrations/versions/0001_fleet_library_baseline.py`
- Test: `control/tests/test_database_authority.py`

- [ ] Write tests for initial head, deterministic previews, persisted previews, successful compare-and-swap, and stale-base rejection.
- [ ] Run the focused tests and verify they fail because the database authority service does not exist.
- [ ] Implement the models, fresh baseline schema, canonical revision hashing, allowlisted document validation, persisted proposals, and transactional change application.
- [ ] Run the focused tests and verify they pass.

### Task 2: Replace API Git construction with database authority

**Files:**
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/dashboard.py`
- Modify: `control/src/vonk_control/fleet_projection.py`
- Modify: `control/src/vonk_control/reconcile.py`
- Modify: `control/src/vonk_control/settings.py`
- Modify: `control/src/vonk_control/worker_authority.py`
- Test: `control/tests/test_generation_readiness.py`
- Test: `control/tests/test_admin_api.py`
- Test: `control/tests/test_worker_authority.py`

- [ ] Add failing startup/API tests proving no repository path or Git key is required.
- [ ] Replace Git repository/proposal/policy construction with the database authority and database change service.
- [ ] Bind dashboard, fleet, update topology, reconciliation, and worker authority to the database revision provider.
- [ ] Remove Git-only settings validation and production requirements.
- [ ] Run focused API and authority tests.

### Task 3: Remove Git runtime code and Compose dependencies

**Files:**
- Delete: `control/src/vonk_control/repository.py`
- Delete: `control/src/vonk_control/proposals.py`
- Delete: `control/src/vonk_control/code_host.py`
- Delete: `control/src/vonk_control/git_policy.py`
- Modify: `deploy/compose/compose.yaml`
- Modify: `deploy/compose/.env.example`
- Modify: `deploy/compose/compose.step-ca.yaml`
- Test: `deploy/compose/tests/test_agent_ingress.py`
- Test: `deploy/compose/tests/test_networking.py`

- [ ] Remove imports and endpoint mappings for Git-only runtime services and rename authority fields from commit/authority_revision to revision/authority_revision.
- [ ] Remove repository bind mounts, repository GID, Git signing secret, and Git environment variables from Compose.
- [ ] Keep only database, secrets, and required credential/publication volumes in the production-shaped graph.
- [ ] Verify rendered Compose has no `/repository`, `REPOSITORY_PATH`, or Git signing input.

### Task 4: Remove obsolete tests and documentation

**Files:**
- Delete: Git repository/policy tests that only test removed runtime behavior.
- Modify: `deploy/compose/README.md`
- Modify: `tests/runbooks/test_nas_compose.py`
- Modify: `control/tests/security/test_no_routine_ssh.py`

- [ ] Replace host-checkout installation instructions with the PostgreSQL-only persistence model.
- [ ] Remove documentation tests that only assert obsolete Git checkout instructions.
- [ ] Run the complete control and Compose test suites.

### Task 5: Publish and validate the deployment bundle

**Files:**
- Modify: `/mnt/z/vonk-forge/docker-compose.yaml` when the repository copy is available.
- Modify: `/mnt/z/vonk-forge/.env` when the repository copy is available.

- [ ] Render the bundle with the target Docker Compose version.
- [ ] Verify no host repository path or Git secret is required.
- [ ] Run focused and full verification before claiming completion.
