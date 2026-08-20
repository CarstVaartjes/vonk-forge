# Tasks 1–2 report: canonical fresh runtime

## Delivered

- Canonical Compose now always includes Step CA; the built-in and Step CA
  overlay files are removed. Hermes is the only opt-in profile.
- The canonical model has an exact default service set and one additional
  Hermes service. Contract coverage rejects missing readiness checks,
  one-shot services, `service_completed_successfully` dependencies,
  non-digest image inputs, and absolute site secret paths.
- Development and production render the same fully resolved Compose model.
  Their only permitted difference is the supplied immutable API/worker image
  identities. The development launcher is exercised against a fake Docker
  boundary and proven to invoke the canonical graph with digest-pinned
  published images.
- Production rendering now expands the same source graph as development and
  ships the same bind-mounted runtime assets.
- The Compose environment uses paths relative to the uploaded directory;
  no NAS-specific `/srv/...` site path remains.
- Related fresh-runtime foundation changes required by this graph are included:
  Step CA settings/secrets, healthchecks, Hermes persistent volumes, Caddy
  readiness/TLS inputs, and first-boot LiteLLM PostgreSQL initialization.

## Explicit cross-task dependency

`control-bootstrap` remains present. A strict expected-failure contract test
records that Tasks 1–2 deliberately retain this privileged bootstrap service
and its dependencies; Task 3 owns removing it. No Task 3 implementation is
included in this commit.

## Verification

- `19 passed, 1 xfailed` for the focused Tasks 1–2 model/renderer/launcher
  suite.
- Canonical and Hermes-profile Compose configuration validation passed with
  `deploy/compose/tests/test.env`.
- Targeted Ruff and shell syntax checks passed.
- A later broad focused command was interrupted by the user after start, so it
  is not claimed as completed. An earlier combined focused run also exposed a
  non-reproducible PyYAML interpreter segmentation fault; immediate isolated,
  pairwise, and repeated reruns passed.
