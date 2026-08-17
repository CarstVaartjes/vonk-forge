# `vonkctl` control API client

`vonkctl` is the routine command-line client for the authenticated control API.
It reads server projections and invokes current Fleet, Catalog, Library, and
platform-update operations. It does not read a local profile controller,
construct SSH workflows, or fall back to direct node access.

Configure an HTTPS origin and a restrictive bearer-token file:

```bash
export VONK_CONTROL_URL=https://control.example.invalid
export VONK_CONTROL_TOKEN_FILE=/run/secrets/vonk-control-token
```

Useful read-only commands:

```bash
uv run --project /path/to/vonk-forge vonkctl nodes status --json
uv run --project /path/to/vonk-forge vonkctl endpoint ALIAS --json
uv run --project /path/to/vonk-forge vonkctl admin fleet --json
uv run --project /path/to/vonk-forge vonkctl admin jobs --json
uv run --project /path/to/vonk-forge vonkctl admin audit --json
```

The browser is the normal recipe workflow: open `/library` to inspect current
model-version families, accepted revisions, placement, runtime state, and
recovery actions; open `/catalog` to create or import a draft, resolve an
immutable revision, attach build evidence, and map it to a cluster. The
canonical JSON editor is an advanced section of that same workflow, not a
second authority.

Platform updates are available under `vonkctl admin updates`; recipe placement
and revision changes are driven through the retained Catalog and Library
workflows. Every mutating command consumes a server-issued digest or plan and
returns a durable job or operation identity. Errors are bounded and redacted.
Credentials belong in the token file or runtime secret store, never in recipe
documents or command arguments.

The generated client boundary is checked in. After changing control routes,
run `scripts/generate-control-clients` and verify that the OpenAPI and generated
Python/TypeScript clients are unchanged except for the intended route set.
