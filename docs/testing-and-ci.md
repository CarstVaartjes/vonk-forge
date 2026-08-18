# Testing and CI policy

Vonk Forge keeps pull-request CI small and deterministic. GitHub Actions is a
merge gate, not the place to run every hardware, browser, Docker, or long-lived
acceptance test on every change.

## Required on every pull request

The protected `main` ruleset requires exactly these checks:

| Check | Purpose |
| --- | --- |
| `Ruff` | Lint changed Python files (or the whole tree for a release tag). |
| `Generated control clients` | Rebuild OpenAPI clients and reject generated drift. |
| `PR contract smoke` | Run the focused repository, control-package, and package-page contracts. |

These checks are intentionally bounded. They do not start Docker Compose,
Playwright, real model services, multi-GPU node jobs, or the full Python/web test
matrices.

## Local verification before requesting review

Run the tier that matches the change, then run the complete local suite for a
release-affecting change:

```bash
# Repository and protocol contracts
uv run --frozen pytest -q

# Control-plane/API/worker tests
uv run --project control --frozen --with-editable . pytest -q control/tests

# Browser/admin UX
npm ci --prefix control/web
npm test --prefix control/web -- --run
npm run build --prefix control/web

# Compose and ingress boundaries
uv run --frozen pytest -q deploy/compose/tests

# Release evidence and generated supply-chain inventory
scripts/verify-supply-chain --json
```

Hardware-dependent lifecycle, thermal, NCCL, real model-quality, physical
replacement, and encryption-drill evidence stays on the designated local
hosts. It is never replaced by a green hosted smoke test.

## When the longer jobs run

Container publication and release metadata are protected by the release
environment and external gates. Ordinary pushes do not run CI; a pull request
to `main` runs only the three required checks above. Concurrency cancels
superseded pull-request runs so a stale commit does not consume another
complete check cycle.

If a change needs a longer check, run it locally and attach its bounded report
to the pull request. Use `workflow_dispatch` only when hosted evidence itself
is required.
