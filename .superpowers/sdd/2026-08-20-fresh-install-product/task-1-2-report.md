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

## Fix round 2

- Embedded runtime config mounts use a fail-safe program rule: every `.sh`
  runtime asset and every other source-executable file renders with mode
  `0555`; remaining data/config files render with mode `0444`. Source ownership
  and writable permission bits are never projected into the deployment.
- Exact rendered mount coverage checks the Caddyfile (`0444`) and Caddy shell
  entrypoint (`0555`), the LiteLLM bootstrap config (`0444`) and executable
  entrypoint/supervisor (`0555`), Tailscale's shell configurator (`0555`), and
  PostgreSQL's executable initializer (`0555`). Caddy and Tailscale currently
  invoke these files through `/bin/sh`; making every `.sh` independently
  executable keeps the rendered artifact robust if invocation changes.
- A real container start probe was not feasible because `docker info` reported
  no available daemon. Compose-model validation was performed with the exact
  requested Compose release instead.

### Verification

- RED: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest -q
  scripts/tests/test_render_dev_compose.py::test_render_preserves_runtime_asset_executability_with_safe_config_modes`
  first reported missing mode metadata, then reported `1 failed` when the
  source-mode-only implementation rendered the Caddy shell entrypoint as
  `0444` instead of the fail-safe `0555`.
- GREEN: the same command reported `1 passed in 0.13s` after applying the
  `.sh` program rule.
- Focused renderers were run in isolated processes because of the known PyYAML
  native-extension flake. `uv run --python 3.12 --frozen --with pytest==9.1.1
  pytest -q scripts/tests/test_render_dev_compose.py` reported `4 passed in
  0.57s`; the equivalent command for
  `scripts/tests/test_render_production_compose.py` reported `6 passed in
  0.33s`.
- `/tmp/vonk-compose-verify.PH64q5/docker-config/cli-plugins/docker-compose
  version` reported `Docker Compose version v5.1.3`.
- That v5.1.3 binary ran `--env-file deploy/compose/tests/test.env -f
  "$round2_final_root/docker-compose.yaml" config -q` and the identical command with
  `--profile hermes`; both exited 0. `find` reported only
  `docker-compose.yaml` in the rendered deployment root.

### PostgreSQL addendum evidence

- `git ls-files --stage deploy/compose/postgres/init-databases.sh
  deploy/compose/tests/test_postgres_init_databases.py` reported both paths as
  tracked at HEAD: executable script blob
  `66adaf0b4d3cd74a9e955e347df42d22a491f0b1` and test blob
  `f9c5f52a3b0d6f78c999c9d7b5d3eca3bd49c6a3`.
- `git log -1 -- deploy/compose/postgres/init-databases.sh` identified
  `661d67ac4d14a76a4f823677942ce158b4df1b94` (`Unify canonical Compose
  runtime`). The equivalent command for the behavior test identified
  `5ac43b6a2412233085ffafbf545c38b5b55b3185` (`fix: make rendered compose
  portable`). No duplicate initializer script is part of this fix round.
