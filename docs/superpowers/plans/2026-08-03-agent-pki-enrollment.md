# Agent PKI and Enrollment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enroll immutable GPU node identities with single-use grants and issue, rotate, and revoke short-lived mTLS certificates through a replaceable CA provider.

**Architecture:** A provider interface isolates PKI implementation. The built-in provider signs from a protected intermediate for zero-dependency bootstrap/development, while a separate Smallstep `step-ca` container is the recommended production provider; both keep the root offline. Caddy authenticates established agents and forwards a stripped, verified identity to private API routes.

**Tech Stack:** Python 3.12, cryptography, FastAPI, PostgreSQL, Smallstep `step-ca`, Caddy 2, Docker Compose, pytest

## Global Constraints

- The root CA private key is never mounted into a running service.
- The intermediate signing key and enrollment grants are secret-file or hashed database material, never Git content or job payloads.
- Grants are single-use, short-lived, node-bound, and stored only as SHA-256 digests.
- Agent certificates are short-lived, identify exactly one canonical node ID, and are denied immediately after revocation/retirement.
- Headers representing mTLS identity are accepted only from the private Caddy ingress and are stripped from untrusted requests.

---

### Task 1: CA provider and built-in issuer

**Files:**
- Create: `control/src/vonk_control/pki.py`
- Modify: `control/pyproject.toml`
- Modify: `control/uv.lock`
- Test: `control/tests/test_pki.py`

**Interfaces:**
- Produces `CertificateAuthority.issue_node(node_id, public_key_pem, now) -> IssuedCertificate`, `renew_node(...)`, `revocation_bundle(now) -> bytes`.
- `IssuedCertificate` contains certificate PEM, chain PEM, serial, fingerprint, not-before, and not-after; never a private key.

- [ ] **Step 1: Write failing issuance tests**

```python
def test_issued_certificate_is_short_lived_and_node_bound(authority, public_key, now) -> None:
    issued = authority.issue_node(NODE_ID, public_key, now)
    certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
    assert certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == NODE_ID
    assert certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH])
    assert certificate.not_valid_after_utc - now == timedelta(hours=24)
```

- [ ] **Step 2: Run and observe missing provider**

Run: `uv run --project control pytest control/tests/test_pki.py -v`
Expected: FAIL importing `vonk_control.pki`.

- [ ] **Step 3: Implement provider and strict built-in issuer**

Pin `cryptography`. Require Ed25519 intermediate key/certificate files to be
regular non-symlinks with matching public keys, `CA=true`, path length zero,
and at least seven days remaining. Issue 24-hour client-auth-only certificates
whose SAN URI is `spiffe://vonk-forge.local/node/<node_id>`. Reject caller-supplied
subjects/SANs and public keys other than Ed25519.

- [ ] **Step 4: Run PKI tests and static import smoke**

Run: `uv run --project control pytest control/tests/test_pki.py -v && uv run --project control python -c 'from vonk_control.pki import BuiltinCertificateAuthority'`
Expected: PASS.

- [ ] **Step 5: Commit provider**

```bash
git add control/pyproject.toml control/uv.lock control/src/vonk_control/pki.py control/tests/test_pki.py
git commit -m "feat: issue short-lived GPU node agent certificates"
```

### Task 2: Enrollment grant service

**Files:**
- Modify: `control/src/vonk_control/models.py`
- Create: `control/migrations/versions/0004_agent_enrollment.py`
- Create: `control/src/vonk_control/enrollment.py`
- Test: `control/tests/test_enrollment.py`

**Interfaces:**
- Produces `EnrollmentService.create(node_id, actor, ttl_seconds) -> EnrollmentGrant`, `submit(token, csr, evidence) -> PendingEnrollment`, `approve(enrollment_id, actor) -> IssuedCertificate`, `reject(...)`, `renew(node_id, serial, csr)`.

- [ ] **Step 1: Write failing replay and approval tests**

```python
def test_grant_is_single_use_and_requires_approval(service) -> None:
    grant = service.create(NODE_ID, "admin", 600)
    pending = service.submit(grant.token, csr(), evidence())
    assert pending.state == "pending-approval"
    with pytest.raises(EnrollmentDenied, match="consumed"):
        service.submit(grant.token, csr(), evidence())
    issued = service.approve(pending.id, "admin")
    assert issued.node_id == NODE_ID
```

- [ ] **Step 2: Run and observe missing service/migration**

Run: `uv run --project control pytest control/tests/test_enrollment.py -v`
Expected: FAIL importing the service.

- [ ] **Step 3: Implement hashed grants and evidence-bound approval**

Add `agent_enrollment_grants` and `agent_enrollments`. Generate 32 random bytes,
return base64url once, persist SHA-256 only, and atomically consume on submit.
Bound evidence to node ID, CSR public-key fingerprint, host-key fingerprint,
hardware fingerprint, agent digest, and boot ID. Require administrator approval,
write certificate metadata, and make approval/rejection idempotent.

- [ ] **Step 4: Test expiry, concurrency, revocation, and renewal**

Run: `uv run --project control pytest control/tests/test_enrollment.py control/tests/test_pki.py -v`
Expected: PASS including simultaneous replay where exactly one submit succeeds.

- [ ] **Step 5: Commit enrollment service**

```bash
git add control/src/vonk_control/models.py control/migrations/versions/0004_agent_enrollment.py control/src/vonk_control/enrollment.py control/tests/test_enrollment.py
git commit -m "feat: enroll immutable GPU node agent identities"
```

### Task 3: Enrollment and agent-authenticated API routes

**Files:**
- Create: `control/src/vonk_control/agent_api.py`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/auth.py`
- Test: `control/tests/test_agent_api.py`
- Test: `control/tests/security/test_agent_identity.py`

**Interfaces:**
- Produces operator endpoints `/api/agents/enrollments*` and agent endpoints `/agent/enroll`, `/agent/claim`, `/agent/heartbeat`, `/agent/result`, `/agent/renew`, and `/agent/artifacts/{sha256}`.
- Consumes verified node identity via ASGI scope populated only by trusted proxy middleware.

- [ ] **Step 1: Write failing authorization tests**

```python
def test_spoofed_agent_header_is_rejected(client) -> None:
    response = client.post("/agent/claim", headers={"x-vonk-agent-node": NODE_ID})
    assert response.status_code == 401

def test_verified_identity_cannot_claim_other_node(agent_client) -> None:
    response = agent_client(NODE_A).post("/agent/claim", json={"node_id": NODE_B})
    assert response.status_code == 403
```

- [ ] **Step 2: Run and observe missing routes**

Run: `uv run --project control pytest control/tests/test_agent_api.py control/tests/security/test_agent_identity.py -v`
Expected: FAIL with 404 responses.

- [ ] **Step 3: Implement separate human and agent authentication dependencies**

Human routes keep token/session RBAC. Agent routes require a private middleware
injected identity object containing node ID, serial, verification marker, and
certificate fingerprint. Enrollment accepts only grant + CSR + bounded evidence.
Claim/result bodies cannot override authenticated node identity. Add mutation
RBAC for administrator enrollment approval/revocation. Artifact retrieval is
mTLS-only, requires the authenticated node to own a live operation referencing
that exact digest, resolves only a regular content-addressed file beneath the
configured artifact root, supports bounded range requests, and never accepts a
caller-selected path or upstream URL.

- [ ] **Step 4: Run API and existing authorization suites**

Run: `uv run --project control pytest control/tests/test_agent_api.py control/tests/security/test_agent_identity.py control/tests/security/test_authorization_matrix.py -v`
Expected: PASS.

- [ ] **Step 5: Commit APIs**

```bash
git add control/src/vonk_control/agent_api.py control/src/vonk_control/api.py control/src/vonk_control/auth.py control/tests/test_agent_api.py control/tests/security/test_agent_identity.py
git commit -m "feat: expose authenticated GPU node agent API"
```

### Task 4: Caddy mTLS boundary and Compose secrets

**Files:**
- Modify: `deploy/compose/Caddyfile`
- Create: `deploy/compose/caddy/entrypoint.sh`
- Modify: `deploy/compose/compose.yaml`
- Create: `deploy/compose/compose.step-ca.yaml`
- Create: `deploy/compose/compose.builtin-ca.yaml`
- Modify: `deploy/compose/.env.example`
- Create: `deploy/compose/step-ca/ca.json`
- Modify: `deploy/compose/tests/test.env`
- Modify: `control/src/vonk_control/settings.py`
- Modify: `control/src/vonk_control/auth.py`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/agent_api.py`
- Modify: `control/src/vonk_control/pki.py`
- Test: `deploy/compose/tests/test_agent_ingress.py`
- Test: `deploy/compose/tests/test_networking.py`
- Test: `control/tests/test_settings.py`
- Test: `control/tests/test_agent_api.py`
- Test: `control/tests/test_pki.py`
- Test: `control/tests/security/test_agent_identity.py`
- Modify: `docs/runbooks/control-plane-bootstrap.md`

**Interfaces:**
- Agent ingress uses a separately configured listener/hostname and client CA.
- Caddy strips all incoming `X-Vonk-Agent-*` headers and supplies verified identity metadata to the API.
- `step-ca` is a separate private-network service with no published port; built-in CA mode remains an explicit deployment profile.

- [ ] **Step 1: Write failing rendered-boundary tests**

Assert agent routes are not reachable through ordinary browser ingress,
client-auth is mandatory except `/agent/enroll`, Caddy is the only published
port, CA/intermediate files are secrets, control-api trusts proxy identity
only on the private ingress network, provider overlays fail closed when
combined in either order, and Caddy/Python canonicalize the proxy secret to
the same single-line base64url-like token.

- [ ] **Step 2: Run and observe absent agent listener**

Run: `uv run pytest deploy/compose/tests/test_agent_ingress.py -v`
Expected: FAIL because no mTLS agent route exists.

- [ ] **Step 3: Implement segmented listener and settings**

Add secret files `agent-client-ca`, `agent-intermediate-certificate`, and
provider credentials; add `VONK_AGENT_*_FILE` and `VONK_AGENT_CA_PROVIDER`
settings. Keep the generic base Compose file provider-neutral and require the
production `compose.step-ca.yaml` overlay for the pinned `step-ca` image,
persistent CA data, health check, no public port, and a separately initialized
offline root/intermediate. The `compose.builtin-ca.yaml` development overlay
may mount the built-in intermediate key without requiring Step CA secrets.
Application settings must reject a merged Step CA/built-in environment
regardless of overlay order. The Caddy entrypoint and Python settings loader
must accept only the same normalized proxy-secret grammar.
Configure Caddy
client authentication and identity forwarding using Caddy placeholders proven
by `caddy validate`. Keep enrollment server-authenticated and rate-limited;
keep claim/result mTLS-only.

- [ ] **Step 4: Validate Caddy and Compose**

Run: `uv run pytest deploy/compose/tests/test_agent_ingress.py deploy/compose/tests/test_networking.py -v && docker compose --env-file deploy/compose/tests/test.env -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml config --quiet && docker compose --env-file deploy/compose/tests/test.env -f deploy/compose/compose.yaml -f deploy/compose/compose.builtin-ca.yaml config --quiet`
Expected: PASS.

- [ ] **Step 5: Commit ingress**

```bash
git add deploy/compose/Caddyfile deploy/compose/caddy/entrypoint.sh deploy/compose/compose.yaml deploy/compose/compose.step-ca.yaml deploy/compose/compose.builtin-ca.yaml deploy/compose/.env.example deploy/compose/step-ca/ca.json deploy/compose/tests/test.env deploy/compose/tests/test_agent_ingress.py deploy/compose/tests/test_networking.py control/src/vonk_control/settings.py control/src/vonk_control/auth.py control/src/vonk_control/api.py control/src/vonk_control/agent_api.py control/src/vonk_control/pki.py control/tests/test_settings.py control/tests/test_agent_api.py control/tests/test_pki.py control/tests/security/test_agent_identity.py docs/runbooks/control-plane-bootstrap.md
git commit -m "feat: authenticate outbound agents through Caddy"
```

### Task 5: PKI recovery and verification

**Files:**
- Create: `control/src/vonk_control/step_ca.py`
- Test: `control/tests/test_step_ca.py`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/settings.py`
- Modify: `deploy/compose/compose.yaml`
- Modify: `deploy/compose/step-ca/ca.json`
- Create: `docs/runbooks/agent-pki.md`
- Modify: `docs/security/threat-model.md`
- Test: `tests/runbooks/test_agent_pki.py`

**Interfaces:**
- Produces `StepCertificateAuthority` behind the Task 1 provider contract,
  constructs the production `AgentApiServices` bundle with the selected CA
  provider and artifact root, and documents offline root creation,
  intermediate rotation, certificate revocation, expiry recovery, provider
  selection, and migration.

- [ ] **Step 1: Write failing runbook behavior checks**

Use a fake `step-ca` HTTP boundary to assert node/SAN/lifetime policy, bounded
responses, renewal/revocation, TLS verification, authenticated provisioner
issuance, and secret redaction. Add production startup tests that construct the
selected `AgentApiServices` bundle only for an explicitly configured provider,
that require CA network reachability for `step-ca`, and that reject unavailable
provider credentials. Parse commands from disposable fixtures and assert no
command copies the root private key into Compose, renewal uses the existing
mTLS identity, and recovery requires an explicit new enrollment grant.

- [ ] **Step 2: Run and observe missing runbook**

Run: `uv run pytest tests/runbooks/test_agent_pki.py -v`
Expected: FAIL because `agent-pki.md` is absent.

- [ ] **Step 3: Write operational PKI and recovery procedure**

Implement the provider with fixed Smallstep sign/renew/revoke requests and a
narrowly scoped provisioner credential loaded from a secret file; never accept
a caller-selected CA URL or certificate subject. Construct and pass the
production `AgentApiServices` bundle in `production_app`, selecting exactly
the configured built-in bootstrap or `StepCertificateAuthority` provider and
its artifact root. Configure the step-ca provisioner and encrypted provider
material through deployment secrets, authenticate every issuance request, and
keep control-api on the private CA network. Include restrictive host secret
permissions and initialization/runbook instructions (Compose bind-backed
secret uid/gid/mode is not portable), offline root storage, intermediate
lifetime and rotation overlap, revocation/retirement, clock-skew checks,
backup scope, and built-in-to-Smallstep provider migration. Explicitly state
that certificate loss does not permit copying another node's identity.

- [ ] **Step 4: Run Phase 2 verification**

Run: `uv run --project control pytest control/tests/test_pki.py control/tests/test_step_ca.py control/tests/test_enrollment.py control/tests/test_agent_api.py control/tests/security/test_agent_identity.py -q && uv run pytest deploy/compose/tests/test_agent_ingress.py tests/runbooks/test_agent_pki.py -q && git diff --check`
Expected: all pass.

- [ ] **Step 5: Commit recovery documentation**

```bash
git add control/src/vonk_control/step_ca.py control/src/vonk_control/api.py control/src/vonk_control/settings.py control/tests/test_step_ca.py deploy/compose/compose.yaml deploy/compose/step-ca/ca.json docs/runbooks/agent-pki.md docs/security/threat-model.md tests/runbooks/test_agent_pki.py
git commit -m "docs: define GPU node agent PKI recovery"
```
