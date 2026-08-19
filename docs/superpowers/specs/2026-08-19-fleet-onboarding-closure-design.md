# Fleet onboarding closure design

**Status:** Approved in chat on 2026-08-19

## Goal

Make Fleet Add Spark usable end to end without changing the enrollment
security model: a valid grant is created, validation failures are actionable,
the generated bootstrap command contains the required deployment inputs, and
the command pins the actual controller CA.

## Decisions

### Grant lifetime

The enrollment grant contract will accept one through 900 seconds. Backend
validation, the enrollment service limit, the frontend request, OpenAPI
metadata, and regression tests will use the named value 900.

### Ingress boundaries

Enrollment and normal agent traffic remain on the existing single Caddy
listener and control-plane deployment. They use separate HTTPS origins on the
same `8443` port:

- the controller/agent origin requires the enrolled Spark client certificate;
- the enrollment origin exposes only the one-time enrollment submission route
  before a client certificate exists.

The control API receives these origins from deployment configuration derived
from the existing agent and enrollment hostnames. No new port is introduced.

### Bootstrap command

The web UI emits a shell-quoted command containing the one-time token, the two
origins, and the CA fingerprint. The canonical Spark installation layout is
used as bootstrap defaults, so the command does not repeat these fixed paths:

- `/etc/vonk-forge-agent/config.json`
- `/var/lib/vonk-forge-agent`
- `/etc/vonk-forge-agent/controller-ca.pem`

The agent bootstrap argument parser will supply those defaults. The one-use
enrollment token remains the direct command value, as requested; no prompt,
temporary token file, or shell-history workaround is added.

### CA fingerprint

The control API will read the deployed public controller CA certificate from
the explicit controller-CA secret/configuration path and calculate its
certificate DER SHA-256 fingerprint. The response will never use a placeholder
or weaken agent verification. The public CA certificate remains the operator's
existing Spark trust input.

### Structured errors

The web API transport will format FastAPI validation arrays and nested detail
objects into bounded human-readable text. It will retain useful `loc` and
`msg` fields while excluding unbounded or sensitive `input` payloads. Plain
string errors continue to render unchanged, and no error may display as
`[object Object]`.

## Scope and interfaces

The change covers:

- control settings, service wiring, grant validation, CA fingerprint
  derivation, deployment secret/environment projections, and backend tests;
- the web Fleet onboarding UI, structured-error formatter, generated API
  contract, and frontend tests;
- agent bootstrap path defaults and parser tests;
- deployment/runbook contract text needed to describe the generated command;
- verification of backend enrollment tests, frontend tests/type checking/build,
  generated-client cleanliness, and the available end-to-end checks.

The change does not add a new enrollment port, weaken mTLS, expose private CA
material, or alter the certificate issuance/approval protocol.

## Acceptance criteria

- Add Spark creates a grant with a 900-second TTL and no 422.
- Structured FastAPI validation details render as bounded actionable text.
- The displayed command includes the token, controller origin, enrollment
  origin, and deployed CA fingerprint, with canonical local paths supplied by
  bootstrap defaults.
- The returned fingerprint matches the configured controller CA certificate's
  SHA-256 fingerprint.
- Bootstrap path defaults remain absolute, safe, and compatible with the
  installed Spark layout.
- OpenAPI and generated TypeScript artifacts are synchronized.
- Existing enrollment idempotency and approval behavior remain covered by
  tests.
