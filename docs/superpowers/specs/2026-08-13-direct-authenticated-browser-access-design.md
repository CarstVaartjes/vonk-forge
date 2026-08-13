# Direct Authenticated Browser Access Design

**Date:** 2026-08-13

**Status:** Approved

## Purpose

Vonk Forge must provide a complete operator journey, not merely a healthy
control API. An authorized operator must be able to open a stable private HTTPS
URL, authenticate in the browser, administer the fleet, and log out without an
SSH tunnel, a PowerShell process, or a bearer token copied into browser tools.

This design completes that journey for the development NAS while preserving the
production trust model. Human ingress is tailnet-only through a Tailscale
Service. Caddy remains the one browser and API gateway. The application owns
administrator authentication, authorization, sessions, and audit identity.

The existing loopback listeners remain available for bounded acceptance and
recovery, but they are not the normal operator path. No human-facing port is
published to the management LAN or the public internet.

## Current gap

The merged system already has several pieces of the intended boundary:

- the API accepts signed bearer tokens and a `vonk_session` cookie;
- mutations using a cookie require a matching CSRF cookie and header;
- PostgreSQL has `users` and `sessions` tables;
- the compiled React interface is served by the control API;
- production Caddy has a private port-8080 browser route;
- the production Tailscale gateway advertises `svc:vonk-forge`; and
- the development Caddy already separates enrollment and mTLS agent SNI on
  port 8443.

The pieces do not form a usable browser login. There is no login, session-status,
or logout API; no credential verifier; no login page; and no development
Tailscale gateway. The development API is therefore reachable by a human only
through loopback forwarding. Documentation nevertheless refers to an
administrator UI, which overstates the current operator experience.

## Chosen approach

The first complete login uses a generated administrator password stored by the
operator in 1Password or another approved encrypted secret store. Vonk Forge
stores only an Argon2id verifier and opaque session digests. This is the smallest
complete and recoverable design that uses the existing user/session model.

Two alternatives are deliberately deferred:

- Trusting forwarded Tailscale identity as application SSO would couple Vonk
  Forge roles to proxy-specific identity headers and recovery semantics.
  Tailscale authorization remains an independent network-reachability gate.
- WebAuthn/passkeys improve phishing resistance but require registration,
  attestation policy, backup, and lost-authenticator recovery. They can be
  added as another credential type without replacing this ingress or session
  design.

## Success criteria

The work is complete only when all of the following are true:

1. An authorized Windows user with Tailscale connected opens the stable
   `svc:vonk-forge` HTTPS URL directly in a normal browser.
2. No SSH, PowerShell forwarding process, manually supplied bearer token, LAN
   admin listener, or TLS warning is required.
3. An unauthenticated browser sees only the login surface and cannot read fleet
   or application API data.
4. The generated `admin` credential from the encrypted operator bundle creates
   a durable server-side session and opens the Fleet page.
5. The interface identifies the authenticated subject, identifies a development
   deployment, and provides logout.
6. Expired, revoked, malformed, or missing sessions return to login cleanly.
7. Logout revokes the server-side session and subsequent reuse fails.
8. Existing CLI and acceptance bearer tokens continue to work.
9. The NAS project still contains only `docker-compose.yml` and `secrets/`;
   existing named volumes, agent identities, repository state, and PostgreSQL
   data survive the upgrade.
10. Fresh-install, normal-update, password-recovery, Tailscale-recovery, and
    troubleshooting documentation describe the exact supported path.

## Trust and network architecture

The request path is:

```text
authorized browser
  -> Tailscale Service svc:vonk-forge HTTPS 443
  -> userspace Tailscale gateway
  -> private tailnet-web-edge Docker network
  -> Caddy HTTP 8080
  -> control-api HTTP 8000
```

Tailscale terminates publicly trusted tailnet HTTPS. Caddy's port 8080 is
available only on the private Docker network and is not published on the host.
The control API retains its loopback-only host mapping on port 8080 for local
acceptance and recovery. LiteLLM retains its loopback-only host mapping on port
4000. Neither mapping is widened.

Development Caddy adds the same browser route shape already used by production:

- `/healthz` is public to the private gateway;
- `/agent/v1/*` and `/internal/*` return 404;
- `/v1/*` routes to LiteLLM;
- `/litellm/*` and `/grafana/*` remain available only when their corresponding
  service is present and retain their own authentication;
- all remaining paths route to the control API; and
- request-size and security-header guards match the production edge.

The existing port-8443 enrollment, agent, and registry SNI boundaries are not
weakened. Browser traffic never enters those sites. Caddy removes untrusted
forwarded identity headers before proxying. Application authentication never
depends on a caller-supplied Tailscale header.

The development gateway reuses the reviewed production topology:

- the official Tailscale image is pinned by digest;
- userspace networking requires no `/dev/net/tun`, host networking,
  `NET_ADMIN`, or `NET_RAW`;
- its root filesystem is read-only apart from bounded runtime mounts;
- its node identity persists in a named volume;
- it joins only the dedicated web-edge network and an egress network;
- it mounts only its own OAuth credential projection; and
- the configurator has the local Tailscale socket but no Docker socket or
  application secrets.

The configurator owns one exact Serve map in development:

```json
{"services":{"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}},"version":"0.0.1"}
```

It resets undeclared, downgraded, plaintext, or retargeted endpoints, advertises
`svc:vonk-forge`, verifies the `service-host` capability, and continuously
reconciles drift. Tailscale Funnel is never enabled.

The implementation follows the current official
[Tailscale Services](https://tailscale.com/kb/1552/tailscale-services),
[Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve), and
[OAuth client](https://tailscale.com/kb/1215/oauth-clients) contracts.

## Tailnet identity and policy

The gateway authenticates with one tailnet-owned OAuth client carrying only
`auth_keys` write scope and `tag:vonk-gateway`. It requests a persistent,
preauthorized tagged node identity only when persisted state is absent or no
longer valid. The client receives no device-management, DNS, policy, key-value,
or broad API scope.

Tailnet policy:

- defines `tag:vonk-gateway` with the designated administrator as owner;
- defines the `svc:vonk-forge` Service;
- allows only the exact administrator identity or administrator group to reach
  `svc:vonk-forge:443`;
- auto-approves only `tag:vonk-gateway` for `svc:vonk-forge`; and
- never uses `svc:*`, an allow-all grant, or a public Funnel rule.

GitHub-backed tailnet identity grants network reachability only. A tailnet user
must still authenticate to Vonk Forge. The OAuth client ID and secret never
enter GitHub, the API, Caddy, PostgreSQL, an image layer, `.env`, or a GPU node.

## Credential generation and storage

Fresh development secret generation creates:

- a 32-byte random administrator password encoded as 43 unpadded base64url
  characters and suitable for 1Password;
- an Argon2id PHC verifier for that password; and
- the existing independent database, signing, PKI, LiteLLM, proxy, and token
  secrets.

The Argon2id policy is explicit and versioned: 64 MiB memory, three iterations,
parallelism one, a 16-byte random salt, and a 32-byte result. Verification uses
the parameters encoded in the PHC string and may request an authenticated
rehash after a future policy increase. Password length is bounded before the
expensive operation to prevent resource-amplification input.

The local source bundle includes both `admin-password` and
`admin-password-verifier`. The publisher excludes plaintext `admin-password`
along with the controller CA private key and public derivations that do not
belong on the NAS. Only `admin-password-verifier` is copied to the NAS and then
projected to the one-shot authentication bootstrap/API boundary. The plaintext
password is stored in 1Password and the encrypted operator backup, never in the
NAS project, Compose environment, logs, command arguments, browser storage, or
Git.

The user supplies `tailscale-oauth-client-id` and
`tailscale-oauth-client-secret` from private mode-0600 local files. The project
publisher validates their shape without printing them and copies them only to
the NAS Tailscale secret projection.

The strict secret-bundle manifest and documented file counts are updated. A
fresh generation contains exactly 21 local source files; the publisher copies
exactly 17 files to the NAS. The four local-only files are `admin-password`,
`controller-ca-key`, `git-signing-key.pub`, and
`host-runtime-grant-public-key`. Generation is all-or-nothing. An existing
valid 17-file source generation gains an explicit add-only browser-access
upgrade mode that:

- preserves every existing secret byte and key relationship;
- accepts the two Tailscale values from private input files;
- generates only the administrator password and verifier;
- refuses incomplete, unknown, symlinked, or partially upgraded state; and
- leaves a complete generation that is backed up before publication.

The SMB publisher retains its lock, journal, tombstone, atomic replacement,
and exact-project-root guarantees. The NAS destination still ends with exactly
`docker-compose.yml` and `secrets/`.

## User and password model

Migration `0021` adds a nullable `String(255)` `password_verifier` field to
`users` without invalidating existing rows. A verifier is nullable so existing
bearer-token and offline installations remain operable while an administrator
credential is explicitly established. A disabled user or a user without a
verifier cannot log in. Browser login accepts only the exact ASCII subject
`admin`; there is no case folding or alias.

Development bootstrap is idempotent and exact:

- it creates subject `admin` with role `administrator` and the supplied
  verifier when the row is absent;
- it accepts an exact existing verifier;
- it refuses an unexpected existing subject or role rather than silently
  replacing authority; and
- a changed verifier from an explicitly rotated and republished source bundle
  atomically replaces only that user's verifier and revokes all of that user's
  sessions.

A one-shot `dev-auth-init` service performs this operation after migration. It
joins only the internal data network, mounts a projection containing only the
database URL and administrator verifier, and exits before the API starts. The
API, worker, Caddy, and Tailscale gateway do not receive the plaintext password;
the authentication initializer receives no Git, PKI, LiteLLM, OAuth, signing,
or agent secret.

Production uses the same model through an offline `create-admin` or
`reset-admin-password` operation that reads the password from protected stdin.
Production never receives the development bootstrap verifier.

Password comparison uses Argon2id's constant-time verifier. Login failure does
not disclose whether a subject exists, is disabled, lacks a verifier, supplied
the wrong password, or was rate-limited. Logs contain only a bounded reason
code, request ID, and outcome; they never contain credentials, PHC strings,
cookies, authorization headers, or request bodies.

## Login throttling

The login boundary applies both per-subject and global bounded throttles before
performing Argon2 work. Subject keys use a keyed digest rather than logging or
retaining the submitted name. The initial policy allows five failed attempts
per normalized subject in five minutes and twenty failures globally in five
minutes. A successful login clears only that subject's failure state.

Throttling is deliberately an application-level defense in depth, not the
primary exposure boundary. Only tailnet-authorized users can reach the endpoint.
The first implementation keeps bounded state in the single API process; API
restart clears it but does not weaken Tailscale authorization, password entropy,
or audit recording. A future multi-replica control plane must move the same
contract to a shared store before adding replicas.

## Session model

Successful authentication generates independent random values:

- a 32-byte opaque session token; and
- a 32-byte CSRF token.

Only the SHA-256 digest of the opaque session token is stored in
`sessions.digest`. The row references the user, records an absolute expiry, and
uses the existing `revoked_at` field. The raw token is never stored in
PostgreSQL or logs. Sessions expire after 12 hours; there is no sliding renewal
in the first version.

The response sets:

- `vonk_session=<opaque>` with `Secure`, `HttpOnly`, `SameSite=Strict`,
  `Path=/`, and `Max-Age` bounded to the server expiry; and
- `vonk_csrf=<random>` with `Secure`, `SameSite=Strict`, `Path=/`, and the same
  bounded lifetime, but without `HttpOnly` because the browser client must copy
  it into `X-CSRF-Token`.

The API resolves cookie authentication through the durable session row and
current user state. Disabling a user takes effect immediately. Mutating requests
retain the exact cookie/header CSRF comparison. Bearer tokens continue through
the existing signed-token verifier and do not create browser sessions.

Logout requires a valid session plus CSRF, marks the current row revoked, and
expires both cookies. Password rotation revokes all sessions for the user.
Expired and revoked rows are rejected immediately and removed by bounded
periodic cleanup.

## Authentication API

The unauthenticated surface is limited to:

- `POST /api/v1/auth/login` with a bounded JSON subject/password document; and
- `GET /api/v1/auth/session`, which returns an authenticated session summary or
  HTTP 401 without disclosing user records.

`POST /api/v1/auth/logout` requires the current cookie session and CSRF token.
The login subject is bounded to 64 ASCII bytes and the UTF-8 password to 256
bytes before any expensive verification. Login and session status return HTTP
200 with only the subject, role, and expiry required by the interface. Login
returns no bearer token. Logout returns HTTP 204.

Login accepts only an HTTPS same-origin browser request: `Origin` must equal the
effective Tailscale HTTPS scheme and request host propagated by the private
Caddy route. Caddy removes caller-supplied forwarding headers before setting
the effective values. The request boundary rejects oversized, malformed,
duplicate, or unexpected fields before password verification. Responses use
`Cache-Control: no-store`. Authentication routes never redirect to
caller-controlled URLs.

## Browser interface

The React application begins in an explicit authentication state:

1. `GET /api/v1/auth/session` checks the current cookie.
2. HTTP 401 renders the login page and no administrative shell.
3. A successful login refreshes session state and opens Fleet.
4. A later API 401 clears client authentication state and returns to login.

The login page uses the established Vonk Forge visual system, labels the site
as private cluster control, identifies development deployments, supports
password managers, and provides clear generic failure and throttling messages.
It never stores a password or session token in JavaScript state longer than the
submission, `localStorage`, `sessionStorage`, IndexedDB, or a URL.

The authenticated shell displays the subject and role, a visible Development
environment marker, and Logout. Logout waits for server confirmation, clears
local state, and returns to login. API clients retain same-origin credentials
and CSRF behavior. All 401 handling is centralized so individual pages do not
render cascades of authentication errors.

## Startup and health behavior

Application services remain independently diagnosable:

- PostgreSQL, migration, repository initialization, and runtime initialization
  complete before the API;
- authentication bootstrap completes before the API reports ready;
- Caddy becomes healthy only with the exact private browser and agent routes;
- the Tailscale gateway becomes healthy only after authentication and socket
  readiness; and
- the configurator becomes healthy only when the exact HTTPS Service map is
  active and advertised.

An OAuth failure or missing Service approval leaves human ingress unavailable
without stopping the API, worker, agents, or inference routes. An absent or
invalid administrator verifier prevents browser login and is reported by a
specific non-secret readiness reason. It never falls back to unauthenticated
access or a LAN listener.

The configurator records the stable Service DNS name as non-secret operational
output so the Docker UI and runbook can show the exact browser URL. It does not
invent a `.lan` hostname or require a Windows hosts-file entry.

## Upgrade and recovery

### Existing development NAS

The operator first upgrades and backs up the local source secret generation,
then republishes the project. In the existing NAS Docker project the operator
chooses Pull and Redeploy while preserving every named volume. The cohort gate,
migration, and authentication bootstrap complete before the new API starts.

No PostgreSQL, repository, route, supervisor, agent-identity, or model cache
volume is removed. The existing agent CAs and node certificates remain valid.

### Password rotation or loss

Normal rotation explicitly generates a new password/verifier, updates the
encrypted backup and 1Password, republishes the verifier, and invokes the
authorized rotation path. All existing browser sessions are revoked.

If the password is lost, recovery requires control of the encrypted operator
bundle or the root-owned offline control-host boundary. The API is stopped, an
offline password-reset command reads a new password from protected stdin,
updates the verifier, revokes sessions, and exits before the API restarts. There
is no email reset, security question, default password, or unauthenticated setup
route.

### Tailscale state loss

Ordinary restarts reuse the persistent gateway node identity. If state is lost,
the scoped OAuth client creates one replacement tagged identity and exact
Service auto-approval restores advertisement. The operator verifies one current
gateway, revokes any orphan, and confirms the Serve map. Application data and
sessions are unaffected, although existing browser connections reconnect.

OAuth revocation prevents creation of a new gateway but does not invalidate
healthy persisted state. A suspected compromise revokes the OAuth client,
gateway node, tag approval, password sessions, and administrator password as
separate authorities.

### Break-glass loopback

Loopback SSH forwarding remains documented only for bounded acceptance and
recovery. It is not described as login, normal access, or a prerequisite for
using Vonk Forge. Recovery never widens Docker host bindings or adds a temporary
LAN listener.

## Publication and deployment

The normal development-image GitHub Actions workflow builds and tests the API,
worker, web assets, Compose artifact, and pinned Tailscale/Caddy inputs from an
accepted `main` commit. Publication remains fail-before-push: tests, scans, and
artifact acceptance complete before mutable `:dev` aliases move.

The NAS receives no GitHub or GHCR credential. Both Vonk Forge images remain
public. Updating the existing project means Pull, then Redeploy; restarting
without Pull is not an update. Production continues to use the signed,
digest-pinned host updater rather than the development mutable-image path.

## Verification strategy

### Authentication and database

Automated tests prove:

- Argon2id generation, verification, malformed-verifier rejection, input bounds,
  and policy rehash detection;
- idempotent administrator bootstrap and fail-closed authority mismatch;
- opaque session creation, digest-only storage, expiry, revocation, disabled
  users, and cleanup;
- exact cookie attributes and no-store headers;
- CSRF on logout and every authenticated mutation;
- generic wrong-user, wrong-password, disabled-user, and throttled responses;
- per-subject/global throttles without plaintext subject retention; and
- unchanged bearer-token behavior.

Migration tests upgrade both an empty database and a populated pre-0021
database without altering existing users or sessions.

### Browser

Component and live-equivalence tests prove:

- unauthenticated startup renders only login;
- password submission is not persisted;
- successful login opens Fleet and shows subject, role, and Development;
- expired/revoked sessions return to login from any page;
- CSRF is sent for logout and mutations;
- logout prevents session reuse; and
- keyboard, focus, password-manager, narrow-screen, and error states remain
  usable.

### Compose and edge

Static and container tests prove:

- the Tailscale image and all Actions are digest pinned;
- no human host port, Funnel, `/dev/net/tun`, host network, Docker socket,
  privileged mode, or network capability is present;
- gateway, configurator, Caddy, and API secrets are disjoint;
- Tailscale state is persistent;
- the exact HTTPS-only `svc:vonk-forge` map is reconciled;
- browser Caddy rejects agent/internal routes and routes application paths;
- port 8443 retains enrollment/mTLS behavior; and
- the generated Compose project still contains only its Compose file and
  secret directory.

### End-to-end acceptance

Before completion, the published `:dev` cohort is deployed to the existing NAS
without deleting volumes. Evidence must show:

1. all long-running services healthy and all one-shot services exited zero;
2. both Sparks retain the same certificate-bound node identities and fresh
   inventory;
3. the exact Tailscale Service is advertised over HTTPS;
4. an unauthorized tailnet identity is denied by policy;
5. an authorized Windows browser opens the stable URL without a tunnel;
6. invalid credentials fail generically;
7. the 1Password administrator credential logs in and Fleet shows both Sparks;
8. an authenticated read and one safe audited administrative preview succeed;
9. logout revokes access; and
10. closing all terminals does not affect browser availability.

The fresh-install runbook is then replayed against an empty disposable project
or equivalent isolated acceptance environment. Passing tests alone do not
substitute for the real browser and deployed-NAS evidence.

## Documentation deliverables

The implementation updates:

- the repository quick start so normal browser access is not described as an
  SSH tunnel;
- the development NAS installation runbook with OAuth creation, tailnet policy,
  secret upgrade, URL discovery, login, update, and recovery;
- the fresh development installation guide with the complete browser journey;
- the Tailscale runbook with the shared development/production Service contract;
- secret inventories, exact file counts, ownership, backup, and rotation rules;
- troubleshooting for OAuth, policy approval, Serve drift, Caddy, login,
  sessions, and lost password; and
- acceptance documentation distinguishing normal Tailscale access from
  break-glass loopback forwarding.

## Non-goals

This slice does not:

- expose Vonk Forge publicly or through Tailscale Funnel;
- make a LAN admin listener;
- use Tailscale identity as the application administrator identity;
- implement passkeys, OIDC, multi-factor authentication, invitations, or
  self-service account creation;
- give LiteLLM, Grafana, Hermes, agents, or GPU nodes the administrator
  credential;
- alter the trusted production platform-update path; or
- remove bounded loopback acceptance and recovery listeners.

These exclusions do not narrow the required outcome: the completed system must
still provide direct, authenticated, persistent browser administration through
Tailscale and Caddy with a reproducible fresh-install path.
