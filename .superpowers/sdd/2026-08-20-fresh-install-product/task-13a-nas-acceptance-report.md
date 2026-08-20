# Task 13A NAS Acceptance Report

**Status:** DONE_WITH_CONCERNS

## Commits and scope

- `1df4b496` — `feat: accept NAS installer candidates behaviorally`
- This report is committed separately after it records that implementation SHA.

Implementation files:

- `.github/workflows/installer-publication.yml`
- `scripts/install-release-publication`
- `scripts/build-nas-compose-bundle`
- `tests/acceptance/runtime.py`
- `tests/acceptance/test_fresh_nas_install.py`
- `tests/test_acceptance_runtime.py`
- `tests/scripts/test_install_release_publication.py`
- `tests/scripts/test_build_nas_compose_bundle.py`
- `tests/test_installer_publication_workflow.py`
- `docs/superpowers/specs/2026-08-20-fresh-install-product-design.md`
- `docs/superpowers/plans/2026-08-20-fresh-install-product.md`

Unrelated dirty files in the worktree were not staged or committed.

## Requirements mapping

| Requirement | Evidence |
| --- | --- |
| Candidate artifacts precede acceptance and the pointer advances only after signed acceptance | Candidate, NAS/Spark acceptance, aggregate acceptance, and promote jobs form parsed dependency chain; publication helper tests exercise candidate, acceptance, and promotion receipts. |
| NAS does not own Docker/Compose | NAS acceptance removes the Docker/Compose runtime version check. The design/plan state that Engine and Compose are NAS-owned; only CI parser fixtures are versioned. |
| UGREEN plus lower Compose compatibility | CI downloads official standalone Compose v5.1.3 and v2.24.6 executables by versioned URL, verifies pinned SHA-256 values, and invokes both against generated default and Hermes bundles. |
| Generated YAML NAS compatibility | The canonical payload now supplies top-level `name: vonk-forge` and `version: "3.9"`; parsed payload test confirms no include/build and pinned images remain. The only permitted parser output is Compose v5's known warning that the required `version` field is obsolete. |
| Clean workstation and interactive wizard | PTY acceptance creates a constrained PATH without Docker, Git, SSH, sudo, or NAS tooling; it generates two clean default bundles and one Hermes bundle. |
| Bundle, repeatability, secret protection, and upgrade preservation | Acceptance enforces the exact `docker-compose.yaml`, `.env`, `secrets/` contract, secure modes, no metadata secret leakage, repeatable release-controlled output, and reruns the first bundle in place to prove byte-for-byte site-secret preservation. |
| One reference rollout, service/topology/route checks, and teardown | The candidate acceptance performs empty-volume `docker compose up -d --wait`, exact health/service checks for default and Hermes, TLS/database/Tailscale checks, and isolated `down --volumes --remove-orphans`. Parser fixtures are not used for rollouts. |
| Behavioral workflow tests | Workflow tests load YAML and assert job dependencies, fixture env bindings, permissions, artifact requirements, report artifacts, and native platform matrix. Source-grep-only workflow assertions were removed. |
| 1Password/key custody ruling | No 1Password/OIDC CI runtime dependency was added and protected GitHub environment execution copies remain. The design records the required future 1Password recoverable copy plus fingerprint-verified encrypted offline escrow, and that the current GitHub-only key must remain untouched until replacement custody passes end-to-end sign/verify. |

## Red/green evidence

1. `test_compose_compatibility_exercises_every_declared_parser_fixture` first failed because `assert_compose_compatibility` did not exist. It passed after the helper invoked every supplied executable with `-f docker-compose.yaml config --quiet`.
2. Parsed workflow acceptance first failed with `KeyError: 'Download verified Compose parser fixtures'`. It passed after the workflow added the checksum-verified UGREEN and lower parser fixture step and paths.
3. `test_payload_is_complete_self_contained_and_fresh_install_only` first failed with `KeyError: 'name'`. It passed after the canonical payload emitted required `name` and `version` fields.
4. `test_compose_compatibility_allows_only_the_required_version_diagnostic` first failed because the helper rejected v5's required-version warning. It passed after accepting only that known diagnostic.
5. `test_interactive_runner_can_allow_upgrade_prompts_to_be_unchanged` first failed with `TypeError` for the missing `require_all_prompts` option. It passed after the PTY helper gained the opt-out used only for the in-place upgrade preservation check.

## Verification commands and outputs

```text
$ uv run pytest tests/test_acceptance_runtime.py tests/scripts/test_build_nas_compose_bundle.py tests/scripts/test_install_release_publication.py tests/test_installer_publication_workflow.py -q
.............................................                            [100%]
45 passed in 10.59s

$ uvx --from ruff==0.16.1 ruff check --force-exclude tests/acceptance/runtime.py tests/acceptance/test_fresh_nas_install.py tests/test_acceptance_runtime.py tests/scripts/test_build_nas_compose_bundle.py tests/scripts/test_install_release_publication.py tests/test_installer_publication_workflow.py scripts/build-nas-compose-bundle
All checks passed!

$ git diff --check
(exit 0; no output)

$ uv run python -c '<parse installer-publication.yml and assert candidate acceptance topology>'
workflow YAML parsed: candidate acceptance topology present

$ docker-compose-v5.1.3 -f docker-compose.yaml config --quiet
time="..." level=warning msg="... the attribute `version` is obsolete, it will be ignored ..."

$ docker-compose-v2.24.6 -f docker-compose.yaml config --quiet
(exit 0; no output)
```

The last two commands used an actual locally rendered, candidate-shaped payload
with official release binaries fetched from Docker Compose's v5.1.3 and v2.24.6
release URLs. SHA-256 verification succeeded before either parser ran. The
version warning is expected because the NAS compatibility contract requires a
top-level `version`; the helper rejects all other parser output.

## CI-only/full-rollout gaps

No actual candidate URL, protected canary secrets, Tailscale tailnet, R2
publication credentials, or isolated CI Docker project are available locally.
Consequently, the following are CI-only and were not claimed as locally run:

- immutable candidate publication and unchanged-pointer observation;
- the literal curl bootstrap against a published candidate;
- real wizard execution and in-place upgrade against that candidate;
- default/Hermes empty-volume rollouts, service health, PostgreSQL/LiteLLM,
  controller TLS, Tailscale gateway/configurator, tailnet HTTPS, observability,
  and registry route checks; and
- signed aggregate acceptance and promotion.

The workflow is arranged so the gate report is written only after the Python
acceptance command succeeds under `set -euo pipefail`; aggregate acceptance and
promotion remain downstream of those reports.

## External 1Password/OIDC provisioning

External key-custody work remains required. Create a replacement private key,
store its canonical recoverable copy in 1Password, create encrypted offline
escrow, prove both derive the tracked public fingerprint, and run end-to-end
sign/verify before changing protected GitHub environment execution copies. The
current GitHub-only key and all external secrets were left untouched. No
1Password/OIDC runtime dependency was added to CI.

## Self-review and concerns

- Reviewed the staged Task 13A paths and kept unrelated web, Caddy, ingress,
  runbook, and other worktree edits unstaged.
- The acceptance helper deliberately permits only Compose v5's `version`
  deprecation diagnostic; this is the compatibility trade-off imposed by the
  required top-level version field.
- The full canary acceptance cannot be validated without the protected external
  infrastructure listed above. It must run successfully before promotion.
