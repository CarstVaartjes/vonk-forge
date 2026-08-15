# Development LiteLLM key-management design

Date: 2026-08-15

## Goal

The development NAS stack must issue persistent, model-restricted LiteLLM
virtual keys without giving workstations the proxy master key. LiteLLM's
official virtual-key contract requires PostgreSQL. Development therefore gains
a dedicated LiteLLM role and database inside the existing PostgreSQL service,
while retaining the current `docker-compose.yml` plus `secrets/` installation
shape and all existing service-secret boundaries.

## Secret and database boundary

Fresh local secret generations include one additional mode-`0600` source file,
`litellm-database-password`, containing exactly 64 lowercase hexadecimal
characters plus one newline. Existing complete generations gain it through the
explicit, idempotent `--upgrade-litellm-key-management` operation. The upgrade
accepts only the exact previous generation, creates one new file with
`O_EXCL`, fsyncs the directory, and validates the complete result. A retry after
an interruption either installs the absent file or validates the already
completed generation.

The offline development initializer creates two disjoint projections:

- `dev-litellm-database-secrets`, readable only by UID/GID `10001:10001`,
  contains the control database URL and the dedicated LiteLLM password for the
  one-shot database initializer;
- `dev-litellm-secrets`, readable only by LiteLLM UID/GID `10002:10001`,
  contains the proxy master key, upstream key, and generated
  `postgresql://litellm:<password>@postgres:5432/litellm` URL.

The API, worker, migration, Caddy, Tailscale, and authentication services do
not mount either new authority outside their stated projection. No password or
database URL is placed in Compose environment values, image layers, logs, or
committed files.

## Startup order

An API-image one-shot named `dev-litellm-database-init` runs as `10001:10001`
on only the internal data network after PostgreSQL is healthy and `dev-init`
has completed. It connects with the projected control database URL, then
idempotently:

1. creates the login role `litellm` or rotates its password to the accepted
   source value;
2. creates database `litellm` owned by that role, or rejects an existing
   database with a different owner;
3. confirms the final role and ownership state before exiting zero.

Identifiers are constants and the password is passed through Psycopg's SQL
literal composition. The secret is never interpolated into a logged command.
LiteLLM starts only after this one-shot exits zero. Its validated supervisor
materializes the database URL marker from the read-only secret projection
instead of deleting that setting. LiteLLM owns its schema migrations and
virtual-key tables inside only the `litellm` database; it has no credential for
the control database. The runtime uses the official signed LiteLLM `v1.96.2`
multi-platform release by immutable index digest, a dedicated executable cache
tmpfs under its non-root UID, and LiteLLM's safer v2 migration resolver while
retaining a read-only container root.

## Client keys

The local-only LiteLLM management endpoint remains authenticated by the master
key. A Pi key is generated with:

- `models: ["mia-deepseek-v4-flash"]`;
- `allowed_routes: ["openai_routes"]`;
- a descriptive key alias.

The Tailscale-facing Caddy route exposes OpenAI-compatible `/v1/*` traffic, not
LiteLLM management routes. The Pi receives only its virtual key. The master key
stays in operator/NAS storage. Revocation uses the local management endpoint.

## Publication and upgrades

The generic runtime-project publisher copies 18 deployment secrets after this
change. The NAS project root still contains exactly `docker-compose.yml` and
`secrets/`. Normal pull/redeploy preserves the PostgreSQL volume and all
virtual keys. A fresh installation executes the database initializer before
LiteLLM; an existing installation first upgrades and republishes the secret
bundle, then pulls and redeploys the accepted Compose artifact.

## Verification

Acceptance requires:

- secret-generation and interrupted-retry tests for the new source file;
- projection ownership, mode, disjointness, and consumer-boundary tests;
- database initializer tests for create, password rotation, wrong owner, and
  unsafe input failures;
- Compose dependency/network/user/read-only tests;
- supervisor tests proving the URL marker is materialized only from the
  expected secret path;
- disposable-stack proof that LiteLLM connects, `/key/generate` succeeds, the
  restricted key can call its allowed model, and the key cannot call a
  management route;
- physical NAS proof followed by a request through the Tailscale gateway.
