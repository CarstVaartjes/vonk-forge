# Compose Control Plane Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a portable Docker Compose control plane with a versioned API, durable PostgreSQL jobs, worker leases, authentication, Caddy ingress, secrets, backups, and offline recovery CLI.

**Architecture:** One custom Python application is packaged once and run as `control-api` and `control-worker`. Standard infrastructure remains in separate containers. PostgreSQL is the durable operational store and job queue; Caddy is the only exposed HTTP entry point.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, PostgreSQL 17, Docker Compose, Caddy 2, pytest, testcontainers or disposable Compose integration.

## Global Constraints

- Run on generic Docker/Compose-capable Linux; UGREEN DXP480T is the first target, not a coded platform.
- Pin every production image by digest and document upgrades.
- Expose only Caddy; PostgreSQL, API, worker, and Caddy admin stay private.
- Use PostgreSQL transactional job claims; add no message broker.
- API and worker use the same image and domain package.
- Secrets enter through mounted secrets/provider references, never Compose environment values committed to Git.
- All mutations are authenticated, authorized, correlated, and audited.

---

### Task 1: Scaffold shared control application and reproducible image

**Files:**
- Create: `control/pyproject.toml`
- Create: `control/src/vonk_control/__init__.py`
- Create: `control/src/vonk_control/settings.py`
- Create: `control/Dockerfile`
- Create: `control/tests/test_settings.py`
- Create: `deploy/compose/compose.yaml`
- Create: `deploy/compose/.env.example`

**Interfaces:**
- Produces: `Settings.from_env_and_secrets()` with database URL file, repository path, state path, and deployment mode.
- Image entrypoints: `python -m vonk_control.api` and `python -m vonk_control.worker`.

- [ ] **Step 1: Write failing secret-file and platform-neutral tests**

```python
def test_database_secret_is_read_from_file(tmp_path, monkeypatch):
    secret = tmp_path / "database-url"
    secret.write_text("postgresql+psycopg://control:pw@postgres/control")
    monkeypatch.setenv("VONK_DATABASE_URL_FILE", str(secret))
    assert Settings.from_env_and_secrets().database_url.host == "postgres"


def test_compose_has_no_host_or_node_host_bindings(compose_text):
    assert "ugreen" not in compose_text.lower()
    assert "192.168." not in compose_text
```

- [ ] **Step 2: Run and verify control package is absent**

Run: `uv run --project control pytest control/tests/test_settings.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement settings, non-root multi-stage image, and initial API/worker services**

Use a locked control-specific environment, read secrets from `*_FILE`, reject raw secret environment counterparts in production, run as numeric non-root UID, use a read-only root filesystem and explicit writable mounts, and define health checks.

- [ ] **Step 4: Validate tests and Compose rendering**

Run: `uv run --project control pytest control/tests/test_settings.py -v && docker compose -f deploy/compose/compose.yaml config --quiet`
Expected: PASS.

- [ ] **Step 5: Commit foundation**

```bash
git add control deploy/compose
git commit -m "feat: scaffold portable control plane"
```

### Task 2: Add PostgreSQL schema and migrations

**Files:**
- Create: `control/src/vonk_control/db.py`
- Create: `control/src/vonk_control/models.py`
- Create: `control/alembic.ini`
- Create: `control/migrations/env.py`
- Create: `control/migrations/versions/0001_operational_state.py`
- Create: `control/tests/test_migrations.py`

**Interfaces:**
- Tables: `jobs`, `job_attempts`, `audit_events`, `observations`, `reconciliations`, `users`, `sessions`.
- Repository definitions and desired documents are deliberately absent.

- [ ] **Step 1: Write failing upgrade/downgrade and authority-boundary tests**

```python
def test_migrations_round_trip(database):
    upgrade(database, "head")
    assert {"jobs", "job_attempts", "audit_events", "observations", "reconciliations"} <= tables(database)
    downgrade(database, "base")
    assert "jobs" not in tables(database)


def test_database_has_no_model_or_profile_authority(database):
    upgrade(database, "head")
    assert not ({"models", "profiles", "desired_profiles"} & tables(database))
```

- [ ] **Step 2: Run against disposable PostgreSQL and observe missing migrations**

Run: `uv run --project control pytest control/tests/test_migrations.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement typed SQLAlchemy models and reversible migration**

Use UUID primary keys, UTC timestamps, JSONB only for bounded redacted payloads, foreign keys, unique request IDs, job state checks, attempt fencing indexes, and retention indexes.

- [ ] **Step 4: Run migration tests**

Run: `uv run --project control pytest control/tests/test_migrations.py -v`
Expected: PASS.

- [ ] **Step 5: Commit database**

```bash
git add control/src/vonk_control/db.py control/src/vonk_control/models.py control/alembic.ini control/migrations control/tests/test_migrations.py
git commit -m "feat: add control plane operational database"
```

### Task 3: Implement durable fenced jobs and worker

**Files:**
- Create: `control/src/vonk_control/jobs.py`
- Create: `control/src/vonk_control/worker.py`
- Create: `control/tests/test_jobs.py`
- Create: `control/tests/test_worker.py`

**Interfaces:**
- `JobService.enqueue(kind, actor, authority_revision, targets, payload) -> Job`.
- `claim(worker_id, lease_seconds) -> JobAttempt | None`, `heartbeat`, `succeed`, `fail`, `wait_for_operator` require matching attempt fence.

- [ ] **Step 1: Write failing claim, expiry, and restart tests**

```python
def test_workers_cannot_claim_same_job(job_service):
    job_service.enqueue("probe", ACTOR, COMMIT, TARGETS, {})
    claims = concurrently_claim(job_service, workers=4)
    assert sum(claim is not None for claim in claims) == 1


def test_stale_attempt_cannot_publish_success(job_service):
    first = claim_then_expire(job_service)
    second = job_service.claim("worker-2", 30)
    with pytest.raises(StaleAttempt):
        job_service.succeed(first)
    job_service.succeed(second)
```

- [ ] **Step 2: Run and observe missing job service**

Run: `uv run --project control pytest control/tests/test_jobs.py control/tests/test_worker.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `FOR UPDATE SKIP LOCKED`, leases, and handler registry**

Persist a redacted payload digest, attempt integer, lease owner/deadline, progress, and terminal result. On restart, verify expired mutating steps before retry. Unknown job kinds fail without execution.

- [ ] **Step 4: Run concurrency and restart tests**

Run: `uv run --project control pytest control/tests/test_jobs.py control/tests/test_worker.py -v`
Expected: PASS.

- [ ] **Step 5: Commit worker**

```bash
git add control/src/vonk_control/jobs.py control/src/vonk_control/worker.py control/tests/test_jobs.py control/tests/test_worker.py
git commit -m "feat: run durable fenced control jobs"
```

### Task 4: Add authenticated, role-authorized control API

**Files:**
- Create: `control/src/vonk_control/api.py`
- Create: `control/src/vonk_control/auth.py`
- Create: `control/src/vonk_control/audit.py`
- Create: `control/tests/test_api.py`
- Create: `control/tests/test_auth.py`

**Interfaces:**
- API prefix `/api/v1`; endpoints `/healthz`, `/readyz`, `/fleet`, `/jobs`, `/jobs/{id}`.
- Roles: viewer, operator, administrator.

- [ ] **Step 1: Write failing authorization and audit tests**

```python
def test_viewer_cannot_enqueue_mutation(client, viewer_token):
    response = client.post("/api/v1/jobs", headers=viewer_token, json=MUTATION)
    assert response.status_code == 403


def test_admin_mutation_has_actor_request_commit_and_targets(client, admin_token, audit_store):
    response = client.post("/api/v1/jobs", headers=admin_token, json=MUTATION)
    event = audit_store.for_request(response.headers["x-request-id"])
    assert (event.actor, event.authority_revision, event.targets) == ("admin", COMMIT, TARGETS)
```

- [ ] **Step 2: Run and verify API is absent**

Run: `uv run --project control pytest control/tests/test_api.py control/tests/test_auth.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement versioned API, session/token verification, RBAC, and redacted audit middleware**

Use secure HttpOnly SameSite cookies for browser sessions and scoped bearer tokens for CLI/automation. Enforce CSRF on cookie-authenticated mutations, request size limits, UUID request IDs, explicit response models, and safe error bodies.

- [ ] **Step 4: Run API/security tests**

Run: `uv run --project control pytest control/tests/test_api.py control/tests/test_auth.py -v`
Expected: PASS.

- [ ] **Step 5: Commit API**

```bash
git add control/src/vonk_control/api.py control/src/vonk_control/auth.py control/src/vonk_control/audit.py control/tests/test_api.py control/tests/test_auth.py
git commit -m "feat: expose authorized control API"
```

### Task 5: Add Caddy-only ingress and network isolation

**Files:**
- Modify: `deploy/compose/compose.yaml`
- Create: `deploy/compose/Caddyfile`
- Create: `deploy/compose/tests/test_networking.py`

**Interfaces:**
- Public ports: Caddy HTTPS and optional HTTP redirect only.
- Private upstreams: `control-api`, later `litellm` and `grafana`; Caddy admin listens only on private network.

- [ ] **Step 1: Write failing rendered-Compose isolation tests**

```python
def test_only_caddy_publishes_ports(rendered_compose):
    published = {name for name, service in rendered_compose["services"].items() if service.get("ports")}
    assert published == {"caddy"}


def test_database_has_no_ingress_network(rendered_compose):
    assert set(rendered_compose["services"]["postgres"]["networks"]) == {"data"}
```

- [ ] **Step 2: Run and observe missing Caddy configuration**

Run: `uv run pytest deploy/compose/tests/test_networking.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement segmented ingress, application, and data networks**

Pin Caddy by digest, disable public admin, apply TLS/security headers/body limits, proxy `/api` and UI only to API, add health-dependent startup, and use read-only config mounts.

- [ ] **Step 4: Validate tests and Compose config**

Run: `uv run pytest deploy/compose/tests/test_networking.py -v && docker compose -f deploy/compose/compose.yaml config --quiet`
Expected: PASS.

- [ ] **Step 5: Commit ingress**

```bash
git add deploy/compose
git commit -m "feat: add isolated Caddy control ingress"
```

### Task 6: Add offline bootstrap, backup, and restore

**Files:**
- Create: `control/src/vonk_control/offline.py`
- Create: `bin/vonk-control-offline`
- Create: `deploy/compose/bin/backup-control-plane`
- Create: `deploy/compose/bin/restore-control-plane`
- Create: `control/tests/test_offline.py`
- Create: `deploy/compose/tests/test_backup_restore.py`
- Create: `docs/runbooks/control-plane-bootstrap.md`
- Create: `docs/runbooks/control-plane-recovery.md`

**Interfaces:**
- Offline commands: `init`, `migrate`, `create-admin`, `backup`, `restore`, `doctor`.
- Offline mutation requires proving API/worker are stopped and taking a host-local lock.

- [ ] **Step 1: Write failing online-conflict and restore tests**

```python
def test_offline_mutation_refuses_healthy_api(run_offline, healthy_api):
    result = run_offline("migrate")
    assert result.returncode == 3
    assert "control plane is running" in result.stderr


def test_backup_restores_database_and_config(disposable_stack):
    backup = disposable_stack.backup()
    disposable_stack.destroy_volumes()
    disposable_stack.restore(backup)
    assert disposable_stack.audit_count() == 1
```

- [ ] **Step 2: Run and verify tools are absent**

Run: `uv run --project control pytest control/tests/test_offline.py deploy/compose/tests/test_backup_restore.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement restrictive offline lock and encrypted backup manifest**

Back up PostgreSQL custom-format dump, Compose/Caddy/Grafana provisioning, repository mirror metadata, and checksums. Require an external encryption command/key reference, reject plaintext production backups, verify checksums before destructive restore, and restore into a fresh disposable stack before documenting success.

- [ ] **Step 4: Run full foundation integration**

Run: `uv run --project control pytest -v && uv run pytest deploy/compose/tests -v && docker compose -f deploy/compose/compose.yaml config --quiet`
Expected: PASS.

- [ ] **Step 5: Commit recovery foundation**

```bash
git add control bin/vonk-control-offline deploy/compose docs/runbooks/control-plane-bootstrap.md docs/runbooks/control-plane-recovery.md
git commit -m "feat: bootstrap and recover the control plane"
```
