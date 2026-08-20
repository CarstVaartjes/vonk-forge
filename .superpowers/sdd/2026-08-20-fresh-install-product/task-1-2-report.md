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

## Fix round 1

- Rendered development and production Compose files now contain the complete
  resolved graph and embed every source-owned runtime bind asset as Compose
  `configs.content`; the deployment root needs no copied Caddy, Grafana,
  LiteLLM, PostgreSQL, Prometheus, registry, or Tailscale files.
- Rendering resolves every service image before serialization. API, worker,
  Hermes, and infrastructure images are digest pinned in the rendered file;
  operator image environment overrides cannot alter that result.
- The production release renderer receives the immutable Hermes release image.
  Disposable development artifact validation supplies its digest-pinned Hermes
  test identity as well.
- The NAS runbook now uses the rendered Hermes identity, relative secret paths,
  Docker named Hermes volumes, and no `/srv` Hermes or `HERMES_DATA_ROOT`
  instructions.
- The no-Git Dockerfile hardening/SBOM pair and the PostgreSQL initializer
  behavior test are included with their claimed implementation.

### Verification

- Focused renderer/contract suite: `23 passed, 1 xfailed` (the expected Task 3
  bootstrap-removal contract).
- `docker --config "$verify_root/docker-config" compose version` reported
  `Docker Compose version v5.1.3`.
- `docker --config "$verify_root/docker-config" compose --env-file
  deploy/compose/tests/test.env -f "$verify_root/docker-compose.yaml" config
  -q` exited 0 for the default graph and the identical command with
  `--profile hermes` exited 0. The rendered directory contained only
  `docker-compose.yaml`.
- The combined Python run can intermittently terminate in the installed
  PyYAML C extension while parsing Compose YAML. Isolated renderer and
  Compose-v5.1.3 checks completed successfully; no source change was made for
  that interpreter-level flake in this round.
