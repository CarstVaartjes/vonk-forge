# Task 13A NAS Acceptance Report

**Status:** DONE

## Commits and scope

- `1df4b496` — behavioral candidate acceptance foundation
- `123a7d34` — original acceptance evidence report
- `b0311e45` — NAS acceptance hardening fix round
- `c77f3342` — route and workflow-to-authority gate coverage

This report revision is committed separately. Only Task 13A paths were staged;
unrelated worktree edits remain unstaged.

## Current requirements mapping

| Requirement | Current evidence |
| --- | --- |
| Candidate before promotion | Parsed workflow dependency graph keeps candidate publication before NAS/Spark acceptance, signed aggregate acceptance, and pointer promotion. |
| NAS Docker boundary | NAS bootstrap path does not install, pin, configure, or require NAS Docker/Compose. CI uses isolated compatibility/reference fixtures only. |
| YAML compatibility | Generated YAML omits top-level `name` and `version`, has no include/build/host bind input, and uses digest-pinned images. Every Compose parser diagnostic fails acceptance. |
| Versioned fixtures | Official v5.1.3 and v2.24.6 Compose binaries are SHA-256 verified for config checks. The one CI-only Hermes-superset rollout uses checksum-verified Compose v5.1.3 against a digest-pinned Docker Engine 29.4.3 DIND service. |
| Bundle and workstation contract | PTY path remains constrained; exact three-item bundle, modes, secret isolation, repeatability, and in-place site-secret preservation are asserted. |
| Full rollout behavior | One empty-volume Hermes-superset rollout validates exact healthy service set, TLS, PostgreSQL/LiteLLM separation, Tailscale, authenticated Prometheus metrics, authenticated Grafana user, registry API, and LiteLLM readiness. Default/Hermes and parser fixture compatibility are config-only where no rollout is required. |
| Gate authority | The workflow's parsed NAS gate JSON is accepted by the real authority together with Spark reports; removing a gate fails as incomplete. |
| Key custody | Protected GitHub environment execution copies remain; no 1Password/OIDC CI runtime dependency or external-secret change was made. |

## Current verification

```text
$ uv run pytest tests/test_acceptance_runtime.py tests/test_fresh_nas_acceptance.py tests/scripts/test_build_nas_compose_bundle.py tests/scripts/test_install_release_publication.py tests/test_installer_publication_workflow.py -q
.................................................                        [100%]
49 passed in 11.76s

$ uvx --from ruff==0.16.1 ruff check --force-exclude <Task 13A Python paths>
All checks passed!

$ git diff --check
(exit 0; no output)
```

## Red/green evidence

- The payload test failed when it required absent `name`/`version`; it passes
  after their removal.
- The compatibility test failed when parser stderr was allowed; it passes only
  when every parser diagnostic is rejected.
- The authority test failed when NAS reports contained gates outside the
  authority set; it passes with the complete canonical set.
- The parsed-workflow integration failed until the emitted NAS gate JSON was
  provided; it passes for that exact JSON and rejects a removed gate.
- The one-rollout test failed until Hermes became the sole reference rollout;
  it now passes.

## CI-only gap and self-review

The published-candidate curl, protected canary secrets, tailnet access, R2,
and actual DIND rollout are CI-only and were not run locally. The workflow is
fail-closed: reports are produced after acceptance succeeds, aggregate signing
requires the complete gate set, and promotion is downstream. External 1Password
replacement key and fingerprint-verified encrypted escrow provisioning remain
required; the current GitHub-only key was not changed.

## Superseded historical record

Earlier report revisions incorrectly described generated top-level `name` and
`version` fields and an allowed Compose-v5 version diagnostic. Those were
intermediate defects, not current product behavior; the current mapping and
verification above supersede them.
