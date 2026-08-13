# Direct Authenticated Browser Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a stable Tailscale HTTPS URL where an authorized operator can log in to Vonk Forge with a generated administrator password, use the web administration interface, and log out without an SSH or PowerShell tunnel.

**Architecture:** A pinned userspace Tailscale gateway advertises only `svc:vonk-forge` and forwards HTTPS traffic over a private Docker network to Caddy. Caddy serves the compiled application/API boundary; the API verifies an Argon2id administrator credential, creates opaque digest-only PostgreSQL sessions, and enforces Secure cookies plus CSRF. A strict local secret generator and publisher add the credential verifier and scoped Tailscale OAuth files without copying the plaintext password to the NAS.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/Alembic, PostgreSQL, `argon2-cffi==25.1.0`, React 19, TypeScript, Vitest, Caddy 2, Docker Compose, Tailscale Services, pytest, GitHub Actions.

## Global Constraints

- Human ingress is Tailscale-only: `svc:vonk-forge` HTTPS 443 to `http://caddy:8080`; do not enable Funnel or publish a human LAN port.
- Keep control API and LiteLLM host mappings bound to `127.0.0.1`; they remain acceptance/recovery paths, not normal browser access.
- Preserve the existing enrollment and mTLS agent SNI boundaries on host TCP 8443.
- Pin `argon2-cffi==25.1.0` in both root tooling and the control package; use Argon2id with memory cost 65536 KiB, time cost 3, parallelism 1, salt length 16, and hash length 32.
- The only browser-login subject in this slice is exact ASCII `admin` with role `administrator`.
- Generate a 32-byte random, unpadded-base64url administrator password; plaintext remains local/1Password and never reaches the NAS.
- A complete source bundle has exactly 21 files and a published NAS bundle exactly 17; local-only files are `admin-password`, `controller-ca-key`, `git-signing-key.pub`, and `host-runtime-grant-public-key`.
- Browser sessions use independent 32-byte opaque and CSRF tokens, expire after 12 hours, and store only the opaque token's SHA-256 digest.
- Set `vonk_session` with `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`; set `vonk_csrf` with `Secure`, `SameSite=Strict`, `Path=/` and no `HttpOnly`.
- Retain signed bearer tokens for CLI/acceptance clients and keep cookie mutations CSRF-protected.
- Keep gateway, configurator, Caddy, API, auth initializer, and worker secret projections disjoint.
- Preserve all existing NAS named volumes, agent identities, PKI, repository state, and PostgreSQL data during upgrade.
- Production remains digest-pinned and host-updater mediated; mutable `:dev` images remain development-only.
- Follow red-green-refactor for every production behavior; record the expected failing assertion before implementation.

---

## File responsibility map

- `control/src/vonk_control/passwords.py`: one Argon2id policy and bounded hash/verify API shared by runtime and local tooling.
- `control/src/vonk_control/browser_auth.py`: durable user/session service plus bounded login throttling; no HTTP concerns.
- `control/src/vonk_control/auth_api.py`: strict FastAPI login/session/logout documents, cookies, same-origin check, and audit events.
- `control/src/vonk_control/dev_auth_init.py`: one-shot development administrator bootstrap/rotation after Alembic migration.
- `control/migrations/versions/0021_browser_authentication.py`: nullable user verifier schema transition.
- `control/web/src/auth.tsx`: browser authentication state and centralized 401 transition.
- `control/web/src/pages/login.tsx`: password-manager-compatible login surface.
- `control/src/vonk_control/resources/dev/Caddyfile`: private browser route plus unchanged agent SNI routes.
- `control/src/vonk_control/resources/dev/tailscale-configure.sh`: exact one-Service development Serve reconciler.
- `scripts/dev-runtime-secrets.py`: complete 21-file source generation, add-only upgrade, and explicit password rotation.
- `scripts/dev-runtime-project`: exact 17-file NAS publication.
- `deploy/compose/compose.dev.images.yaml`: auth initializer, Tailscale gateway/configurator, private networks, volumes, and projections.
- `docs/runbooks/development-nas-installation.md` and `docs/runbooks/fresh-development-install.md`: authoritative existing/fresh operator journeys.

---

### Task 1: Pin Argon2 and add the password-verifier schema

**Files:**
- Modify: `pyproject.toml`
- Modify: `control/pyproject.toml`
- Modify: `uv.lock`
- Modify: `control/uv.lock`
- Create: `control/src/vonk_control/passwords.py`
- Modify: `control/src/vonk_control/models.py`
- Create: `control/migrations/versions/0021_browser_authentication.py`
- Create: `control/tests/test_passwords.py`
- Create: `control/tests/test_browser_authentication_migration.py`
- Modify: `control/tests/test_recipe_catalog_migration.py`

**Interfaces:**
- Produces: `hash_password(password: str) -> str`
- Produces: `verify_password(verifier: str, password: str) -> PasswordVerification`
- Produces: `PasswordVerification(valid: bool, needs_rehash: bool)`
- Produces: nullable `User.password_verifier: str | None`
- Consumes: `argon2.PasswordHasher` from exactly `argon2-cffi==25.1.0`

- [ ] **Step 1: Write failing password-policy tests**

Add tests that name the production change explicitly:

```python
def test_hash_password_emits_the_exact_argon2id_policy() -> None:
    verifier = hash_password("A" * 43)
    assert verifier.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    assert verify_password(verifier, "A" * 43) == PasswordVerification(True, False)


@pytest.mark.parametrize("password", ["", "x" * 257])
def test_password_boundary_rejects_empty_or_oversized_input(password: str) -> None:
    with pytest.raises(PasswordPolicyError, match="password is invalid"):
        hash_password(password)


def test_verify_password_returns_one_generic_invalid_result() -> None:
    assert verify_password("not-a-phc-string", "wrong") == PasswordVerification(False, False)
```

- [ ] **Step 2: Run the password tests and verify RED**

Run: `uv run --project control --frozen pytest control/tests/test_passwords.py -q`

Expected: collection fails because `vonk_control.passwords` does not exist.

- [ ] **Step 3: Pin the dependency and implement the exact bounded policy**

Add `argon2-cffi==25.1.0` to both project dependency lists, refresh both lockfiles with `uv lock` and `uv lock --project control`, then implement:

```python
HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


@dataclass(frozen=True)
class PasswordVerification:
    valid: bool
    needs_rehash: bool


def _bounded(password: str) -> str:
    if not isinstance(password, str) or not 1 <= len(password.encode("utf-8")) <= 256:
        raise PasswordPolicyError("password is invalid")
    return password


def hash_password(password: str) -> str:
    return HASHER.hash(_bounded(password))


def verify_password(verifier: str, password: str) -> PasswordVerification:
    try:
        bounded = _bounded(password)
        valid = HASHER.verify(verifier, bounded)
        return PasswordVerification(valid, valid and HASHER.check_needs_rehash(verifier))
    except (PasswordPolicyError, InvalidHashError, VerificationError):
        return PasswordVerification(False, False)
```

- [ ] **Step 4: Write failing migration/model tests**

Assert Alembic head is `0021_browser_authentication`, `users.password_verifier` is nullable `VARCHAR(255)`, an existing user survives upgrade with `NULL`, a PHC verifier survives round-trip, downgrade removes only the new column, and SQLAlchemy metadata matches.

- [ ] **Step 5: Run migration tests and verify RED**

Run: `uv run --project control --frozen pytest control/tests/test_browser_authentication_migration.py control/tests/test_recipe_catalog_migration.py::test_recipe_catalog_is_the_linear_head -q`

Expected: FAIL because revision 0021 and the mapped field are absent.

- [ ] **Step 6: Add the model field and linear migration**

Create revision `0021_browser_authentication` with `down_revision = "0020_recipe_catalog_bridge"`, use a batch alter for SQLite compatibility, and add/drop only `sa.Column("password_verifier", sa.String(255), nullable=True)`.

- [ ] **Step 7: Run focused and lock-integrity verification**

Run:

```bash
uv run --project control --frozen pytest \
  control/tests/test_passwords.py \
  control/tests/test_browser_authentication_migration.py \
  control/tests/test_recipe_catalog_migration.py -q
uv lock --check
uv lock --project control --check
```

Expected: all pass with no lock drift.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml control/pyproject.toml uv.lock control/uv.lock \
  control/src/vonk_control/passwords.py control/src/vonk_control/models.py \
  control/migrations/versions/0021_browser_authentication.py \
  control/tests/test_passwords.py \
  control/tests/test_browser_authentication_migration.py \
  control/tests/test_recipe_catalog_migration.py
git commit -m "feat(auth): add Argon2 administrator verifier"
```

---

### Task 2: Implement durable opaque browser sessions and throttling

**Files:**
- Create: `control/src/vonk_control/browser_auth.py`
- Create: `control/tests/test_browser_auth.py`
- Modify: `control/src/vonk_control/models.py` only if an index/check required by a failing test is absent

**Interfaces:**
- Consumes: `User`, `LoginSession`, `hash_password`, `verify_password`
- Produces: `BrowserIdentity(actor: Actor, expires_at: datetime, session_id: str)`
- Produces: `IssuedBrowserSession(identity: BrowserIdentity, token: str, csrf: str)`
- Produces: `BrowserAuthService.login(subject: str, password: str) -> IssuedBrowserSession`
- Produces: `BrowserAuthService.resolve(token: str) -> BrowserIdentity`
- Produces: `BrowserAuthService.logout(token: str) -> None`
- Produces: `BrowserAuthService.bootstrap_admin(verifier: str) -> BootstrapResult`
- Produces: `BrowserAuthService.rotate_admin(verifier: str) -> BootstrapResult`

- [ ] **Step 1: Write failing session lifecycle tests**

Use an in-memory SQLAlchemy session factory, an aware deterministic clock, and injected token source. Assert:

```python
issued = service.login("admin", ADMIN_PASSWORD)
row = db.scalar(select(LoginSession))
assert row.digest == sha256(issued.token.encode()).hexdigest()
assert issued.token not in repr(row.__dict__)
assert service.resolve(issued.token).actor == Actor("admin", "administrator")
service.logout(issued.token)
with pytest.raises(BrowserAuthenticationError):
    service.resolve(issued.token)
```

Add separate tests for 12-hour expiry, disabled user, missing verifier, malformed token, digest mismatch, password rotation revoking every session, and bounded expired-row cleanup.

- [ ] **Step 2: Run the lifecycle tests and verify RED**

Run: `uv run --project control --frozen pytest control/tests/test_browser_auth.py -q`

Expected: collection fails because `browser_auth` is absent.

- [ ] **Step 3: Implement the minimal durable service**

Use `secrets.token_urlsafe(32)` for both raw values, require the unpadded base64url shape, store `sha256(token).hexdigest()`, query user and session in database transactions, compare aware UTC expiry, and return only typed identities. Never put raw tokens or passwords into exception text or dataclass reprs.

- [ ] **Step 4: Write failing throttle tests**

Assert five failed attempts for one keyed subject in five minutes cause a generic throttle result, twenty failures globally throttle a new subject, a success clears only its subject, the map evicts expired entries, and no submitted subject appears in internal keys or repr output.

- [ ] **Step 5: Run throttle tests and verify RED**

Run: `uv run --project control --frozen pytest control/tests/test_browser_auth.py -k throttle -q`

Expected: FAIL because throttling has not been implemented.

- [ ] **Step 6: Add a bounded single-process limiter**

Implement `LoginRateLimiter` with injected monotonic clock, a keyed HMAC-SHA256 subject identifier derived from the existing token-signing key, a maximum of 1,024 tracked subjects, five subject failures/300 seconds, and twenty global failures/300 seconds. Evict expired entries before admission; reject rather than growing beyond the bound.

- [ ] **Step 7: Add exact bootstrap/rotation behavior**

`bootstrap_admin` creates exact subject/role when absent, accepts an exact verifier, rejects role/subject conflicts, and returns `created` or `unchanged`. `rotate_admin` updates only `admin.password_verifier`, revokes all active rows for that user in the same transaction, and returns `rotated` or `unchanged`.

- [ ] **Step 8: Verify the complete service**

Run: `uv run --project control --frozen pytest control/tests/test_browser_auth.py control/tests/test_passwords.py -q`

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add control/src/vonk_control/browser_auth.py control/tests/test_browser_auth.py \
  control/src/vonk_control/models.py
git commit -m "feat(auth): add durable browser sessions"
```

---

### Task 3: Add the authenticated browser API boundary

**Files:**
- Create: `control/src/vonk_control/auth_api.py`
- Create: `control/tests/test_auth_api.py`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/tests/test_api.py`
- Modify: `control/tests/admin_equivalence_server.py`
- Modify: `scripts/generate-control-clients`
- Modify: `control/openapi.json`
- Modify: `control/web/src/api/generated.d.ts`

**Interfaces:**
- Consumes: `BrowserAuthService`, `AuditSink`, existing `TokenCodec`
- Produces: `install_auth_routes(app, service, audits, actor_dependency)`
- Produces endpoints `POST /api/v1/auth/login`, `GET /api/v1/auth/session`, `POST /api/v1/auth/logout`
- Changes: `create_app(..., browser_auth: BrowserAuthService | None = None)`
- Preserves: `Authorization: Bearer <signed-token>` behavior

- [ ] **Step 1: Write failing API tests for login and cookies**

Cover strict JSON, exact subject, password bounds, same-origin enforcement, generic 401, generic 429, successful response body, `Cache-Control: no-store`, and cookie flags. Inspect raw `Set-Cookie` headers and require:

```text
vonk_session=...; HttpOnly; Max-Age=43200; Path=/; SameSite=strict; Secure
vonk_csrf=...; Max-Age=43200; Path=/; SameSite=strict; Secure
```

Assert no response body or header contains the password, verifier, or raw bearer token.

- [ ] **Step 2: Run login API tests and verify RED**

Run: `uv run --project control --frozen pytest control/tests/test_auth_api.py -q`

Expected: FAIL with HTTP 404 for `/api/v1/auth/login`.

- [ ] **Step 3: Implement strict auth documents and routes**

Use Pydantic `ConfigDict(extra="forbid", strict=True)`. Require `Origin == f"https://{request.headers['host']}"` for login. Emit HTTP 200 session summary on login/status, HTTP 204 on logout, generic `authentication failed` for credential failures, and `authentication temporarily unavailable` for throttling. Append bounded audit actions `auth.login.succeeded`, `auth.login.failed`, `auth.login.throttled`, and `auth.logout` without credential material.

- [ ] **Step 4: Write failing actor-boundary tests**

Assert cookie auth resolves through `BrowserAuthService`, not `TokenCodec`; signed bearer auth remains unchanged; cookie mutations need matching CSRF; disabled/revoked/expired sessions return 401; and a bearer token cannot be mistaken for an opaque cookie session.

- [ ] **Step 5: Run actor tests and verify RED**

Run: `uv run --project control --frozen pytest control/tests/test_api.py -k 'cookie or bearer' -q`

Expected: the existing signed-cookie behavior fails the new durable-session expectations.

- [ ] **Step 6: Integrate the optional browser service into `create_app`**

Resolve bearer headers through `TokenCodec`. Resolve a `vonk_session` only through `browser_auth`; if browser auth is absent, cookie auth is unavailable. Preserve CSRF comparison for every cookie-authenticated non-safe method. Install auth routes only when the service exists, then instantiate `BrowserAuthService` from the production SQLAlchemy session factory and token-signing key in `production_app`.

- [ ] **Step 7: Regenerate and verify the shared API contract**

Run:

```bash
scripts/generate-control-clients
uv run --project control --frozen pytest \
  control/tests/test_auth_api.py control/tests/test_api.py control/tests/test_admin_api.py -q
npm --prefix control/web test -- --run src/api/client.test.ts
```

Expected: generated OpenAPI/types include only the three auth routes and all focused tests pass.

- [ ] **Step 8: Commit**

```bash
git add control/src/vonk_control/auth_api.py control/src/vonk_control/api.py \
  control/tests/test_auth_api.py control/tests/test_api.py \
  control/tests/admin_equivalence_server.py scripts/generate-control-clients \
  control/openapi.json \
  control/web/src/api/generated.d.ts
git commit -m "feat(auth): expose browser login and logout"
```

---

### Task 4: Bootstrap development authentication and extend the strict secret bundle

**Files:**
- Create: `control/src/vonk_control/dev_auth_init.py`
- Create: `control/tests/test_dev_auth_init.py`
- Modify: `control/src/vonk_control/dev_init.py`
- Modify: `control/tests/test_dev_init.py`
- Modify: `scripts/dev-runtime-secrets.py`
- Modify: `scripts/tests/test_dev_runtime_secrets.py`
- Modify: `scripts/dev-runtime-project`
- Modify: `scripts/tests/test_dev_runtime_project.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: private source files for Tailscale OAuth ID/secret
- Produces source secrets: `admin-password`, `admin-password-verifier`, `tailscale-oauth-client-id`, `tailscale-oauth-client-secret`
- Produces NAS secrets: all except plaintext `admin-password`
- Produces: `python -m vonk_control.dev_auth_init` with `VONK_DEV_AUTH_MODE=bootstrap|rotate`
- Changes: development runtime projection roots add `/auth-secrets` and `/tailscale-secrets`

- [ ] **Step 1: Write failing 21/17 bundle tests**

Update expected sets explicitly. Assert fresh generation creates 21 owner-mode-0600 regular files, the password matches `[A-Za-z0-9_-]{43}`, the verifier validates it, OAuth inputs are copied byte-for-byte from safe 0600 files, logs show neither value, and the publisher emits exactly 17 NAS files while excluding the four local-only names.

- [ ] **Step 2: Run secret tests and verify RED**

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest scripts/tests/test_dev_runtime_secrets.py scripts/tests/test_dev_runtime_project.py -q`

Expected: FAIL because the four browser-access source names and three published names are absent.

- [ ] **Step 3: Implement fresh generation and safe external inputs**

Add required CLI options `--tailscale-oauth-client-id-file` and `--tailscale-oauth-client-secret-file`. Open both with the existing no-follow, owner, mode, size, inode-stability checks. Generate `admin-password` with `secrets.token_urlsafe(32)` and hash it through `vonk_control.passwords.hash_password`. Extend validation to verify exact PHC policy and password/verifier relationship without printing either.

- [ ] **Step 4: Write failing add-only upgrade and rotation tests**

Prove `--upgrade-browser-access` accepts only the exact prior 17-file source generation, preserves every old inode/content, adds exactly four files, and refuses partial/unknown state. Prove `--rotate-admin-password` changes only password/verifier, keeps 21 files, requires explicit flag, and leaves the old credential invalid.

- [ ] **Step 5: Run upgrade tests and verify RED**

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest scripts/tests/test_dev_runtime_secrets.py -k 'upgrade_browser or rotate_admin' -q`

Expected: argparse rejects the new flags.

- [ ] **Step 6: Implement recoverable add-only upgrade and rotation**

Reuse directory descriptors and exclusive creation. Preflight the entire state before writing. On any failure, remove only files created by that invocation. Rotation writes and fsyncs two temporary regular files plus a private transaction journal containing only old/new SHA-256 digests, validates the password/verifier relationship, replaces both names, fsyncs the directory, and removes the journal. At startup, recover every interruption point by matching the journal digests and either finish the new pair or restore the old pair; reject any state not named by the journal. Tests must interrupt after journal creation and after each rename, then prove the next invocation repairs the pair without exposing either credential.

- [ ] **Step 7: Write failing projection and one-shot bootstrap tests**

Assert runtime init creates disjoint API, worker, migration, Caddy, LiteLLM, auth, and Tailscale roots. Auth gets exactly `database-url` and `admin-password-verifier`; Tailscale gets exactly the OAuth pair. `dev_auth_init` creates `admin`, is idempotent, rejects role conflict, and `rotate` revokes sessions.

- [ ] **Step 8: Run projection/bootstrap tests and verify RED**

Run: `uv run --project control --frozen pytest control/tests/test_dev_init.py control/tests/test_dev_auth_init.py -q`

Expected: new projection arguments/module are absent.

- [ ] **Step 9: Implement projections and the one-shot module**

Extend `_PROJECTION_FILES` and exact owner/mode checks without sharing projection roots. `dev_auth_init` reads only its two files with no-follow bounded reads, builds the SQLAlchemy factory, selects bootstrap/rotate from the exact environment enum, calls `BrowserAuthService`, prints only `created`, `unchanged`, or `rotated`, and exits nonzero on authority mismatch.

- [ ] **Step 10: Verify all secret/bootstrap contracts**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest \
  scripts/tests/test_dev_runtime_secrets.py \
  scripts/tests/test_dev_runtime_project.py -q
uv run --project control --frozen pytest \
  control/tests/test_dev_init.py control/tests/test_dev_auth_init.py -q
```

Expected: all pass and captured output contains no fixture secrets.

- [ ] **Step 11: Commit**

```bash
git add .gitignore control/src/vonk_control/dev_auth_init.py \
  control/src/vonk_control/dev_init.py control/tests/test_dev_auth_init.py \
  control/tests/test_dev_init.py scripts/dev-runtime-secrets.py \
  scripts/dev-runtime-project scripts/tests/test_dev_runtime_secrets.py \
  scripts/tests/test_dev_runtime_project.py
git commit -m "feat(dev): provision browser access secrets"
```

---

### Task 5: Build the login, authenticated shell, and logout UX

**Files:**
- Create: `control/web/src/auth.tsx`
- Create: `control/web/src/auth.test.tsx`
- Create: `control/web/src/pages/login.tsx`
- Create: `control/web/src/pages/login.test.tsx`
- Modify: `control/web/src/api/client.ts`
- Modify: `control/web/src/api/client.test.ts`
- Modify: `control/web/src/api/types.ts`
- Modify: `control/web/src/app.tsx`
- Modify: `control/web/src/main.tsx`
- Modify: `control/web/src/styles.css`
- Modify: `control/web/src/admin-equivalence.live.test.tsx`

**Interfaces:**
- Produces: `AuthSession {subject, role, expires_at}`
- Produces: `AuthProvider`, `useAuth()`, and `AuthenticationRequired`
- Produces client methods: `session()`, `login(subject, password)`, `logout()`
- Consumes only same-origin HttpOnly cookies; never exposes raw session tokens

- [ ] **Step 1: Write failing unauthenticated-startup and login tests**

Assert initial session check shows a bounded loading state, HTTP 401 renders only the login page, the sidebar/Fleet never render before authentication, fields use `autocomplete="username"` and `autocomplete="current-password"`, subject defaults to `admin`, and a successful login opens Fleet with `admin`, `Administrator`, and `Development` visible.

- [ ] **Step 2: Run UI tests and verify RED**

Run: `npm --prefix control/web test -- --run src/auth.test.tsx src/pages/login.test.tsx`

Expected: tests fail because the auth provider/login page do not exist.

- [ ] **Step 3: Implement the minimal auth state and login page**

Keep the password in component state only until submission, clear it in `finally`, do not persist it, and map both 401 and 429 to bounded non-secret messages. The provider owns session fetch/login/logout and renders either login or children; `App` receives a guaranteed authenticated session.

- [ ] **Step 4: Write failing centralized expiry/logout tests**

Make any API request return 401 and assert the entire shell returns to login once. Assert logout sends `X-CSRF-Token`, waits for HTTP 204, removes shell content, and a failed logout does not pretend server revocation succeeded.

- [ ] **Step 5: Run expiry/logout tests and verify RED**

Run: `npm --prefix control/web test -- --run src/auth.test.tsx src/api/client.test.ts`

Expected: FAIL because 401 propagation and logout are absent.

- [ ] **Step 6: Add one centralized unauthorized signal and shell identity controls**

`ApiClient` throws `AuthenticationRequired` on API 401 and dispatches one in-memory callback; it never reloads recursively. Add authenticated subject/role, a Development badge, and Logout to the sidebar. Preserve existing navigation and page APIs.

- [ ] **Step 7: Add accessible responsive styling**

Create a centered login card matching the existing dark/green visual system; preserve visible labels, focus outlines, reduced narrow-screen layout, error `role="alert"`, disabled submission, and password-manager semantics. Do not add remote fonts, analytics, or third-party scripts.

- [ ] **Step 8: Verify all web behavior and production build**

Run:

```bash
npm --prefix control/web test -- --run
npm --prefix control/web run build
```

Expected: all tests pass and Vite production build succeeds without warnings.

- [ ] **Step 9: Commit**

```bash
git add control/web/src/auth.tsx control/web/src/auth.test.tsx \
  control/web/src/pages/login.tsx control/web/src/pages/login.test.tsx \
  control/web/src/api/client.ts control/web/src/api/client.test.ts \
  control/web/src/api/types.ts control/web/src/app.tsx control/web/src/main.tsx \
  control/web/src/styles.css control/web/src/admin-equivalence.live.test.tsx
git commit -m "feat(web): add administrator login experience"
```

---

### Task 6: Add the private Caddy browser edge without weakening agent ingress

**Files:**
- Modify: `control/src/vonk_control/resources/dev/Caddyfile`
- Modify: `control/src/vonk_control/resources/dev/caddy-entrypoint.sh`
- Modify: `control/tests/test_dev_runtime_assets.py`
- Modify: `deploy/compose/tests/test_dev_complete_stack.py`
- Modify: `deploy/compose/tests/test_agent_ingress.py` if shared edge assertions require it

**Interfaces:**
- Produces: private Caddy `:8080` browser listener for Tailscale only
- Preserves: TLS enrollment/agent listeners on 8443 and exact mTLS identity headers
- Routes: `/v1/* -> litellm:4000`; default -> `control-api:8000`

- [ ] **Step 1: Write failing Caddy route/security tests**

Assert the development Caddyfile has one `:8080` site, blocks `/agent/v1/*` and `/internal/*`, protects LiteLLM repository-authority mutation paths, strips `X-Vonk-Agent-*`, applies edge headers/body limits, and does not add a host port. Retain every existing enrollment/mTLS assertion.

- [ ] **Step 2: Run Caddy tests and verify RED**

Run: `uv run --project control --frozen pytest control/tests/test_dev_runtime_assets.py deploy/compose/tests/test_agent_ingress.py -q`

Expected: FAIL because no development browser listener exists.

- [ ] **Step 3: Add the private browser route using the production pattern**

Copy the reviewed route semantics, not the production file wholesale. Keep Caddy admin health on loopback 2019. Do not add automatic public TLS or expose 8080. Explicitly remove forwarding and agent identity headers before proxying.

- [ ] **Step 4: Extend running-stack acceptance**

From a disposable container on the private edge network, assert `/` serves the login HTML, `/api/v1/auth/session` returns 401 before login, `/agent/v1/claim` returns 404 on the browser site, and the existing real mTLS agent endpoint still reaches the API.

- [ ] **Step 5: Run focused runtime acceptance**

Run: `uv run --project control --frozen pytest control/tests/test_dev_runtime_assets.py deploy/compose/tests/test_dev_complete_stack.py -q`

Expected: all pass; no test skips the Caddy browser assertions.

- [ ] **Step 6: Commit**

```bash
git add control/src/vonk_control/resources/dev/Caddyfile \
  control/src/vonk_control/resources/dev/caddy-entrypoint.sh \
  control/tests/test_dev_runtime_assets.py \
  deploy/compose/tests/test_dev_complete_stack.py \
  deploy/compose/tests/test_agent_ingress.py
git commit -m "feat(edge): route private browser access through Caddy"
```

---

### Task 7: Add the exact development Tailscale Service gateway

**Files:**
- Create: `control/src/vonk_control/resources/dev/tailscale-configure.sh`
- Modify: `control/src/vonk_control/dev_runtime_assets.py`
- Modify: `control/tests/test_dev_runtime_assets.py`
- Modify: `deploy/compose/compose.dev.images.yaml`
- Modify: `deploy/compose/tests/test_dev_compose.py`
- Create: `deploy/compose/tests/test_dev_tailscale.py`
- Modify: `deploy/compose/tailscale/grants.example.hujson`
- Modify: `deploy/compose/tests/test_tailscale.py`

**Interfaces:**
- Consumes: existing pinned Tailscale image and `TS_CLIENT_ID`/`TS_CLIENT_SECRET` file support
- Produces services: `tailscale-gateway`, `tailscale-configurator`
- Produces volumes: `dev-tailscale-state`, `dev-tailscale-socket`
- Produces network: internal `tailnet-web-edge`
- Produces exact map: `svc:vonk-forge` HTTPS 443 -> `http://caddy:8080`

- [ ] **Step 1: Write failing static least-privilege tests**

Assert the gateway has a digest-pinned official image, read-only root, userspace mode, persistent state/socket, exact OAuth projection, `TS_AUTH_ONCE=true`, `tag:vonk-gateway`, no host ports/devices/capabilities/privilege/Docker socket, and only egress plus `tailnet-web-edge`. Assert Caddy joins only its existing networks plus `tailnet-web-edge`.

- [ ] **Step 2: Run static tests and verify RED**

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest deploy/compose/tests/test_dev_tailscale.py -q`

Expected: FAIL because the services are absent.

- [ ] **Step 3: Write a failing fake-CLI reconciler test**

Reuse the production fake socket/CLI strategy but assert exactly one service. Begin with extra/plaintext/retargeted state; require reset, `--service=svc:vonk-forge --https=443 http://caddy:8080`, advertise, final exact config, `service-host`, and no secret output.

- [ ] **Step 4: Run reconciler test and verify RED**

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest deploy/compose/tests/test_dev_tailscale.py -k reconciler -q`

Expected: FAIL because the packaged script is absent.

- [ ] **Step 5: Implement and stage the bounded reconciler**

Add the script to `_ASSETS` as mode 0555 with a 128-KiB cap. It waits at most 120 seconds for the socket, compares whitespace-normalized status/config to the one exact map, resets drift, advertises once, verifies HTTPS and `service-host`, then reconciles every 60 seconds. It must never print OAuth values or use Funnel.

- [ ] **Step 6: Add gateway/configurator Compose services**

Use the exact image lock already tested by production. Mount only the OAuth projection/state/socket. Run configurator with `network_mode: service:tailscale-gateway`, the shared socket, and packaged script. Gateway does not depend on application readiness; configurator waits for Caddy health and gateway health so API/agents remain independently available during tailnet failure.

- [ ] **Step 7: Update the reviewed tailnet policy example**

Keep the production service names but make the exact administrator grant and `autoApprovers.services["svc:vonk-forge"] = ["tag:vonk-gateway"]` contract explicit. Do not broaden Hermes access or add `svc:*`.

- [ ] **Step 8: Verify development and production Tailscale contracts**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest \
  deploy/compose/tests/test_dev_tailscale.py \
  deploy/compose/tests/test_tailscale.py \
  deploy/compose/tests/test_dev_compose.py -q
uv run --project control --frozen pytest control/tests/test_dev_runtime_assets.py -q
```

Expected: all pass and production's three-Service map remains unchanged.

- [ ] **Step 9: Commit**

```bash
git add control/src/vonk_control/resources/dev/tailscale-configure.sh \
  control/src/vonk_control/dev_runtime_assets.py \
  control/tests/test_dev_runtime_assets.py \
  deploy/compose/compose.dev.images.yaml \
  deploy/compose/tests/test_dev_compose.py \
  deploy/compose/tests/test_dev_tailscale.py \
  deploy/compose/tailscale/grants.example.hujson \
  deploy/compose/tests/test_tailscale.py
git commit -m "feat(dev): add private Tailscale browser ingress"
```

---

### Task 8: Wire auth initialization, publication, and complete-stack acceptance

**Files:**
- Modify: `deploy/compose/compose.dev.images.yaml`
- Modify: `deploy/compose/tests/test_dev_compose.py`
- Modify: `deploy/compose/tests/test_dev_compose_secrets.py`
- Modify: `deploy/compose/tests/test_dev_complete_stack.py`
- Modify: `scripts/render-dev-compose`
- Modify: `scripts/tests/test_render_dev_compose.py`
- Modify: `scripts/dev-image-acceptance`
- Modify: `scripts/tests/test_dev_image_acceptance.py`
- Modify: `scripts/verify-dev-image-secrets`
- Modify: `scripts/tests/test_verify_dev_image_secrets.py`
- Modify: `.github/workflows/dev-images.yml`
- Modify: `scripts/tests/test_dev_image_workflow.py`

**Interfaces:**
- Produces Compose service: `dev-auth-init`
- Produces volumes: `dev-auth-secrets`, `dev-tailscale-secrets`, `dev-tailscale-state`, `dev-tailscale-socket`
- Changes API dependency: migration + auth init must complete successfully before startup
- Changes accepted runtime dependency count from three to four pinned third-party images

- [ ] **Step 1: Write failing Compose order/projection tests**

Assert `dev-auth-init` runs the API image as UID 10001, joins only `data`, mounts only auth secrets, waits for PostgreSQL/migrate/dev-init, and exits before API. Assert API waits for it, Tailscale gets only OAuth, no service gets plaintext password, and all old volume names remain present.

- [ ] **Step 2: Run Compose tests and verify RED**

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest deploy/compose/tests/test_dev_compose.py deploy/compose/tests/test_dev_compose_secrets.py -q`

Expected: missing auth/Tailscale projections and dependencies fail.

- [ ] **Step 3: Complete the Compose graph**

Add auth/tailnet volumes and secrets, auth initializer command/environment, health/dependency conditions, and networks. Preserve mutable/pinned render placeholders and project-name behavior. Do not add a `current/` directory, Dockerfile, build context, or Git checkout to the NAS artifact.

- [ ] **Step 4: Write failing renderer/scanner/workflow tests**

Assert mutable and pinned artifacts contain the same auth/Tailscale topology, render no secret values, retain public image aliases/digests as appropriate, preload exactly four pinned third-party runtime images, and run auth/web/Tailscale focused tests before registry login.

- [ ] **Step 5: Run publication tests and verify RED**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest \
  scripts/tests/test_render_dev_compose.py \
  scripts/tests/test_dev_image_acceptance.py \
  scripts/tests/test_verify_dev_image_secrets.py \
  scripts/tests/test_dev_image_workflow.py -q
```

Expected: dependency count/test-list assertions fail.

- [ ] **Step 6: Update render, scan, acceptance, and workflow gates**

Teach acceptance to validate auth/Tailscale files as names and permissions only; never read them into logs. Add focused auth, migration, UI build, Caddy, and Tailscale tests to the pre-build source gate. Keep OCI build, scan, complete-stack acceptance, exact-main recheck, immutable publication, attestation, and mutable alias advancement order unchanged.

- [ ] **Step 7: Extend complete-stack acceptance without contacting a real tailnet**

Start the application/Caddy subset explicitly and test real auth bootstrap, login cookies, Fleet, logout, agent mTLS, and route acknowledgement. Test the Tailscale reconciler separately with its fake socket/CLI; do not give CI live OAuth credentials or make tailnet availability a source acceptance dependency.

- [ ] **Step 8: Run the entire development publication contract**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest -q \
  deploy/compose/tests/test_dev_compose.py \
  deploy/compose/tests/test_dev_compose_secrets.py \
  deploy/compose/tests/test_dev_tailscale.py \
  scripts/tests/test_render_dev_compose.py \
  scripts/tests/test_dev_runtime_secrets.py \
  scripts/tests/test_dev_runtime_project.py \
  scripts/tests/test_dev_image_acceptance.py \
  scripts/tests/test_dev_image_workflow.py
uv run --project control --frozen pytest -q \
  deploy/compose/tests/test_dev_complete_stack.py
```

Expected: all pass; Docker-backed test runs rather than skips when Docker is available.

- [ ] **Step 9: Commit**

```bash
git add deploy/compose/compose.dev.images.yaml deploy/compose/tests \
  scripts/render-dev-compose scripts/dev-image-acceptance \
  scripts/verify-dev-image-secrets scripts/tests \
  .github/workflows/dev-images.yml
git commit -m "feat(dev): publish complete browser access stack"
```

---

### Task 9: Replace tunnel-first documentation with the complete browser journey

**Files:**
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `deploy/compose/README.md`
- Modify: `docs/runbooks/development-nas-installation.md`
- Modify: `docs/runbooks/fresh-development-install.md`
- Modify: `docs/runbooks/tailscale.md`
- Modify: `docs/runbooks/control-plane-recovery.md`
- Modify: `docs/security/threat-model.md`
- Modify: `tests/runbooks/test_development_nas_installation.py`
- Modify: `tests/runbooks/test_nas_compose.py`
- Modify: `tests/test_docs_contract.py`

**Interfaces:**
- Produces authoritative normal URL discovery/login/update/recovery instructions
- Preserves loopback forwarding only under acceptance and break-glass recovery
- Documents exact 21/17 secret names, ownership, 1Password handling, and rotation

- [ ] **Step 1: Write failing documentation contracts**

Require the normal path to name Tailscale Trust credentials, `auth_keys`, `tag:vonk-gateway`, `svc:vonk-forge`, HTTPS-only Serve, OAuth file inputs, 1Password administrator item, stable URL discovery, browser login/logout, Pull then Redeploy, password rotation, state recovery, and no Funnel/LAN port. Reject any quick-start sentence that says an SSH tunnel is normal UI access.

- [ ] **Step 2: Run docs tests and verify RED**

Run: `uv run --frozen pytest tests/runbooks/test_development_nas_installation.py tests/runbooks/test_nas_compose.py tests/test_docs_contract.py -q`

Expected: missing Tailscale/browser requirements fail.

- [ ] **Step 3: Update the existing-install runbook**

Document exact OAuth creation in the Tailscale admin console, safe file capture without shell arguments/output, add-only secret upgrade, encrypted backup, tailnet policy merge, NAS project republish, Pull/Redeploy with volumes preserved, service URL discovery, first login, normal update, logout, and diagnostics. Put any user-required console action in an explicit numbered step.

- [ ] **Step 4: Update the fresh-install path**

Make the shortest supported path end with direct browser login and both Sparks visible. Keep `/etc/hosts` only for enrollment/agent/registry names on NAS/GPU nodes; state clearly that the Tailscale browser URL needs no Windows hosts entry. Retain the SSH tunnel only in deterministic acceptance and recovery sections.

- [ ] **Step 5: Update security, recovery, and root entry points**

Document independent tailnet reachability and application-auth gates, verifier/session/Tailscale secret boundaries, OAuth/state/password compromise responses, and production host-updater separation. Ensure every cross-link points to the authoritative runbook anchor.

- [ ] **Step 6: Run all documentation contracts**

Run: `uv run --frozen pytest tests/runbooks tests/test_docs_contract.py -q`

Expected: all pass with no stale normal-access tunnel claim.

- [ ] **Step 7: Commit**

```bash
git add README.md docs/README.md deploy/compose/README.md \
  docs/runbooks/development-nas-installation.md \
  docs/runbooks/fresh-development-install.md docs/runbooks/tailscale.md \
  docs/runbooks/control-plane-recovery.md docs/security/threat-model.md \
  tests/runbooks tests/test_docs_contract.py
git commit -m "docs: make browser login the supported operator path"
```

---

### Task 10: Verify, review, publish, deploy, and prove the end-user journey

**Files:**
- Modify only files required by failures or review findings
- Produce local ignored evidence under `.state/direct-browser-access/`
- Produce GitHub PR and accepted development Compose artifact
- Update installation evidence documentation only with non-secret final facts

**Interfaces:**
- Consumes all prior task outputs
- Produces passing repository suites, reviewed PR, merged `main`, published `:dev` cohort, upgraded NAS project, and real browser acceptance evidence

- [ ] **Step 1: Run formatting, lock, generated-contract, and secret scans**

Run:

```bash
git diff --check origin/main...HEAD
uv lock --check
uv lock --project control --check
scripts/verify-supply-chain
scripts/verify-dev-image-secrets \
  --expected-commit "$(git rev-parse HEAD)" \
  --expected-repository https://github.com/CarstVaartjes/vonk-forge \
  vonk-forge-api:dev-local vonk-forge-worker:dev-local
npm --prefix control/web run build
```

Expected: zero exits and no generated drift or secret finding.

- [ ] **Step 2: Run focused suites in parallel**

Run the control auth/migration suite, web suite, development Compose/Tailscale suite, secret/publisher suite, and documentation suite as separate concurrent jobs. Record exact commands and exit codes; do not infer broad success from one aggregate command.

- [ ] **Step 3: Run complete repository and Docker acceptance**

Run the repository's authoritative CI-equivalent commands from `docs/testing-and-ci.md`, including the real complete development stack. Investigate every failure systematically; do not waive a skip that covers browser, Caddy, Tailscale reconciler, migration, or secret isolation.

- [ ] **Step 4: Request independent code and security review**

Use `superpowers:requesting-code-review`, review the diff against the approved spec, and address every actionable finding test-first. Re-run the affected focused suite after each fix and the full relevant matrix at the end.

- [ ] **Step 5: Push and open a draft PR**

Use `github:yeet` to confirm scope, push `feature/direct-browser-access`, and open a draft PR. Include the design, plan, test evidence, explicit no-LAN/no-Funnel claim, migration behavior, and manual deployment gates.

- [ ] **Step 6: Wait for and fix GitHub Actions**

Use `github:gh-fix-ci` for any failing check. Merge only after all required checks, image acceptance, scans, and review are green. Do not manually publish images or create a release.

- [ ] **Step 7: Create the scoped Tailscale authority and policy**

In the Tailscale admin console, create or confirm one OAuth client with only `auth_keys` write and `tag:vonk-gateway`; store ID/secret in 1Password. Merge the exact `svc:vonk-forge` grant and auto-approval for the administrator identity. Confirm HTTPS is enabled and Funnel remains disabled. This is an explicit user-console action because it changes external authority.

- [ ] **Step 8: Upgrade the private source bundle without printing secrets**

Read OAuth values from 1Password into mode-0600 files inside the existing private mode-0700 staging directory, invoke `--upgrade-browser-access`, verify 21 names/modes/relationships without content output, create the 1Password `Vonk Forge Development Administrator` item from `admin-password`, and back up the complete source generation encrypted.

- [ ] **Step 9: Publish and redeploy the NAS project**

Download the accepted Compose artifact for the merged 40-character main commit. Run `scripts/dev-runtime-project` against the mounted NAS parent. Verify the destination has only `docker-compose.yml` and `secrets/`. In the existing NAS Docker UI choose Pull then Redeploy, preserving all named volumes; never choose delete-related-images/volumes as a cleanup shortcut.

- [ ] **Step 10: Prove runtime health and identity preservation**

Record service/container health, one-shot zero exits, accepted image cohort, Alembic head, exact Tailscale Serve map, Service URL, Caddy readiness, and both existing Spark node IDs/certificate fingerprints/fresh inventory. Record only hashes, public identities, and status—not credentials or cookies.

- [ ] **Step 11: Prove the Windows browser journey**

With all SSH/PowerShell tunnel processes closed, open the reported Tailscale HTTPS URL in Windows. Verify login-only unauthenticated state, generic invalid-password failure, successful `admin` login via the 1Password item, Fleet showing both Sparks, one safe audited preview, logout, failed back-navigation/session reuse, and continued availability after all terminals close.

- [ ] **Step 12: Replay the fresh-install contract in isolation**

Use an empty disposable Compose project or equivalent isolated acceptance environment. Generate a fresh 21-file source, publish 17 files, start the stack, obtain/approve the Service, log in, log out, then remove only the explicitly disposable project. Preserve the real NAS project and all real named volumes.

- [ ] **Step 13: Final requirement-by-requirement audit**

Compare every success criterion and numbered acceptance item in the approved design to direct evidence. Treat missing, indirect, skipped, or stale evidence as incomplete. Only after all items are proven, update the installation record with bounded non-secret evidence and mark the goal complete.
