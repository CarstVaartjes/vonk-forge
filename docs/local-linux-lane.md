# Local Linux lane: Controller/Spark wire contracts

`control/tests/test_*_wire_bridge.py` is not a JSON-artifact consumer. Each
suite **executes** the Rust example probe that
`scripts/tests/run_agent_wire_contracts.py` builds, pipes Controller-produced
JSON to the probe's stdin, and parses the probe's stdout — for example
`control/tests/test_heartbeat_wire_bridge.py:97` and
`control/tests/test_recipe_job_wire_bridge.py:156`. The probe binaries are
Linux-only (the agent crates use `rustix::fs::openat2`, gated on
`linux_raw_dep`), so a macOS Rust build cannot run them and the whole tier has
to run inside Linux. `scripts/dev-agent-wire-linux` does that in OrbStack with
the same command CI runs.

## When to reach for it

- You changed a wire schema, a `rust/crates/vonk-agent*` payload, or a
  `control/tests/test_*_wire_bridge.py` suite and want local evidence before
  pushing.
- The macOS fast tier reported the bridge suites as an error (a missing probe
  or a `rustix` compile failure) and you need to know whether the failure is
  the platform or the change.
- You are reviewing a wire-contract change and want to run the connected
  producer/consumer boundary, not just one side.

It is deliberately not a substitute for CI, and it is not physical Spark or
model evidence. It also proves nothing about amd64 packaging: the container is
Linux/arm64 on Apple silicon.

## Usage

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes
scripts/dev-agent-wire-linux
```

Useful flags:

- `--check-parity` — assert the CI job and the lane still agree, without Docker.
- `--dry-run` — print the container invocation and exit.
- `--rebuild` — rebuild the lane image (for example after editing the
  Dockerfile).
- `--recipe-library PATH` / `--any-recipe-revision` — override the sibling
  checkout (default `VONK_RECIPE_LIBRARY_ROOT`, else `/opt/vonk-forge-recipes`),
  or accept its being at a revision other than the one CI checks out.
- `--cache-root PATH` — override the persistent cache directory.

## What it guarantees

- **The CI command, not a paraphrase.** It runs
  `uv run --project control --frozen --with-editable . python
  scripts/tests/run_agent_wire_contracts.py -- -q -n 4 --dist loadfile`, the exact
  `controller-spark-wire` step. Before starting Docker it extracts that step,
  the Rust/uv/Python pins and the recipe-revision file from
  `.github/workflows/ci.yml` and refuses to run when they disagree with the
  lane. `tests/scripts/test_dev_agent_wire_linux.py` asserts the same in the
  repository suite, so a CI change that forgets the lane fails in CI.
- **The CI toolchain.** Base `ubuntu:24.04` pinned by index digest
  (`ubuntu:24.04@sha256:224a1869…`) with Python 3.12, uv `0.12.1` and Rust
  `1.97.1` (`--profile minimal --component rustfmt`), mirroring the CI
  `rustup toolchain install` step. `UV_PYTHON_DOWNLOADS=never` keeps uv from
  silently substituting a downloaded interpreter.
- **The pinned recipe library.** `VONK_RECIPE_LIBRARY_ROOT` is bind-mounted
  read-only at `/recipe-library`, and the lane refuses to run unless the
  checkout is at the revision in `tests/acceptance/recipe-library-revision.txt`
  (override with `--any-recipe-revision`). A dirty recipe checkout is reported.
- **No root-owned files in the repository.** The container runs as
  `--user $(id -u):$(id -g)`, the project virtualenv is redirected to the cache
  (`UV_PROJECT_ENVIRONMENT`), and the cargo target directory, registry, uv
  cache and logs all live outside the worktree.
- **No silent success.** A container that starts and skips or errors is not
  evidence: the lane propagates the pytest exit code and also refuses to report
  success when the output has no passing-test summary or says `no tests ran`.
  The runner itself already refuses to proceed when a probe is missing.
- **Acquisition retries before execution.** The lane prepares the locked
  Controller environment through `scripts/retry-dependency-fetch` before its
  wire command, matching CI's dependency preflight. The runner builds every
  selected Rust probe in one Cargo invocation so shared dependencies use one
  feature graph. Four pytest workers distribute whole files. The selected
  probes and pytest assertions are unchanged.
- **A real error when the container cannot run.** With no reachable Docker
  engine (for example `docker context use missing`) it exits non-zero and
  explains that OrbStack must be started, rather than falling back to a macOS
  build that cannot execute the probes.

## Cost

Caches live in `${XDG_CACHE_HOME:-$HOME/.cache}/vonk-forge/agent-wire-linux`
(override with `--cache-root` or `VONK_AGENT_WIRE_CACHE`) and hold the lane
image, the cargo registry and target directory, the uv cache and the project
virtualenv. Every run appends its full container output to
`…/logs/run-<timestamp>.log`.

| Run | Wall clock on this host | Dominated by |
| --- | --- | --- |
| Cold — no image, empty caches | 8m25s and 12m19s on two runs | the one-time image build (~1.5 min with no BuildKit layers), uv resolution, the first `cargo build --locked`, then the suite |
| Historical warm runs before probe batching | 6m40s–7m10s | the sequential pytest suite; cargo is incremental and the image build is skipped |

On 2026-09-12, after probe batching, the same first selection took 64.47s
sequentially and 31.74s with four workers: **674 passed / 17 skipped** in both
runs. The publisher selection also passed (1 test). These are local Linux
measurements, not a promise of hosted CI wall time. The combined Cargo build
also replaces four feature-resolution invocations with one; on the same warm
cache that reduced build overhead from 0.42s to 0.13s.

Timings move with host load and this machine's sleep behaviour. Earlier runs
before probe batching reported 3m44s–7m03s for the first selection and one
passing release-publication test. The bridge suites alone, given the probe environment
`run_agent_wire_contracts.py` builds, are **55 passed, 1 skipped**; the skip is
`test_operation_progress_wire_bridge.py`, which skips unless
`VONK_PROGRESS_WIRE_PROBE` is set and for which the runner builds no probe — CI
skips it too, and this lane deliberately reproduces CI rather than papering over
the gap.

To force a fully cold run, `rm -rf "$XDG_CACHE_HOME/vonk-forge/agent-wire-linux"`
and `docker image rm vonk-forge-agent-wire-linux`. Rebuilding the image while
the BuildKit layer cache is intact takes seconds, so `--rebuild` alone is cheap.

## Limits

- It runs only the job's final wire step, not the host-independent checks that
  precede it in `controller-spark-wire` (recipe launch projections, API contract
  completeness, `generate-agent-wire --check`). Those need no cargo, so the
  macOS fast tier already covers them.
- No systemd, no GPU, no NVIDIA hardware, no physical Spark. It cannot produce
  hardware, NCCL/fabric or model-quality evidence.
- The base image is pinned by tag plus index digest, like the deployment images,
  but this lane is not part of `deploy/compose/images.lock.json`: it carries no
  release artifact. Refresh the pin with
  `docker buildx imagetools inspect ubuntu:24.04` and a reviewed edit to the
  Dockerfile, `scripts/dev_agent_wire_linux.py` and this doc together — the
  lane's parity check refuses a base that is not the recorded pin. If the
  image-pin runbook (owned by the release-pinning work) should also inventory
  this base, that belongs in `docs/image-pin-refresh.md`, not here.
