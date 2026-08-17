# Task 5 report — scrub legacy documentation, repository contracts, and artifacts

Date: Monday, August 17, 2026
Branch: `refactor/clean-slate-control-plane`

## Scope completed

- Rewrote the supported Spark installation/onboarding story to say registration generates the node-specific runtime inputs and a generated bootstrap command.
- Removed the obsolete manual `agent.toml` editing guidance from supported runbooks.
- Deleted tracked historical runbooks/plans that still described the removed Git-roster/manual-agent-TOML workflow.
- Updated runbook tests first, ran the requested suite red, then rewrote docs to green.

## Reference inventory

Exact command run first:

```bash
rg -n "inventory/fleet\.toml|config/package-families|config/workload-deployments|/packages|/deployments|Catalog page|Packages page|Deployments page|Jobs page|edit .*agent\.toml" README.md docs control agent scripts tests packaging
```

Raw command notes:

- The `/packages` and `/deployments` alternates also matched legitimate apt-package URLs, lockfiles, and package-runtime fixtures.
- The supported-source files that mattered for Task 5 were:
  - `docs/operations/install-vonk-agent.md`
  - `docs/runbooks/fresh-development-install.md`
  - `docs/runbooks/development-agent-workloads.md`
  - `docs/runbooks/node-onboarding.md`
  - `docs/runbooks/inventory.md`
  - `docs/superpowers/plans/2026-08-03-per-node-onboarding.md`
  - `docs/superpowers/plans/2026-08-07-vonk-rust-agent-and-debian-package.md`
  - `docs/superpowers/plans/2026-08-10-development-agent-workload-slices.md`
  - `docs/superpowers/plans/2026-08-17-clean-slate-cleanup.md`
  - `docs/superpowers/specs/2026-08-17-fleet-library-product-simplification-design.md`
  - `packaging/config/agent.toml`
  - `tests/runbooks/test_node_onboarding.py`

Post-scrub focused sweep result:

- Remaining supported-source references are limited to:
  - the preserved active cleanup plan,
  - the preserved active Fleet/Library design spec,
  - the rewritten installation/onboarding runbooks,
  - the bootstrap-placeholder package config comment,
  - legitimate apt package-release docs/URLs,
  - test assertions that enforce the new generated-bootstrap wording.

## Changed files

- Modified: `docs/operations/install-vonk-agent.md`
- Modified: `docs/runbooks/fresh-development-install.md`
- Modified: `docs/runbooks/development-agent-workloads.md`
- Modified: `docs/runbooks/node-onboarding.md`
- Modified: `packaging/config/agent.toml`
- Modified: `tests/runbooks/test_node_onboarding.py`
- Modified: `tests/runbooks/test_development_nas_installation.py`
- Added: `tests/runbooks/test_agent_installation.py`

## Deleted files

- Deleted: `docs/runbooks/inventory.md`
- Deleted: `docs/superpowers/plans/2026-08-03-per-node-onboarding.md`
- Deleted: `docs/superpowers/plans/2026-08-07-vonk-rust-agent-and-debian-package.md`
- Deleted: `docs/superpowers/plans/2026-08-10-development-agent-workload-slices.md`

## TDD evidence

Assertions updated before doc changes:

- `tests/runbooks/test_node_onboarding.py`
- `tests/runbooks/test_agent_installation.py`
- `tests/runbooks/test_development_nas_installation.py`

Red run (meaningful failure after fixing the runner invocation):

```text
FAILED tests/runbooks/test_agent_installation.py::test_install_runbook_requires_registration_generated_bootstrap_inputs
FAILED tests/runbooks/test_agent_installation.py::test_fresh_install_runbook_uses_generated_bootstrap_story
FAILED tests/runbooks/test_agent_installation.py::test_development_agent_workloads_runbook_drops_manual_agent_toml_steps
FAILED tests/runbooks/test_node_onboarding.py::test_node_onboarding_runbook_covers_safe_resumable_workflow
4 failed, 51 passed, 1 skipped in 3.40s
```

## Verification commands and exact output

Requested runbook/repository/proposal suite:

```bash
uv run --project control --with-editable . --frozen python -m pytest -q tests/runbooks control/tests/test_repository.py control/tests/test_proposals.py
```

Output:

```text
55 passed, 1 skipped in 3.13s
```

Focused packaged-agent artifact check:

```bash
uv run --frozen python -m pytest -q tests/test_agent_release_workflow.py -k 'agent_package_builds_a_deb_with_both_controller_origins or reusable_agent_package_build_preserves_acceptance_gates'
```

Output:

```text
1 passed, 30 deselected in 0.05s
```

Static diff hygiene:

```bash
git diff --check
```

Output:

```text
[no output]
```

Focused legacy-reference sweep used after the rewrite:

```bash
rg -n "inventory/fleet\.toml|package-candidate|deployment-rollout|generated bootstrap command|Git roster|Git Spark" README.md docs control tests packaging scripts
```

Result summary:

- `generated bootstrap command` appears only in the rewritten supported runbooks/tests.
- `inventory/fleet.toml`, `Git roster`, and `package-candidate` now remain only in the preserved active cleanup plan/spec that describe the removal itself.

## Commit

Commit message required by brief:

```text
docs: scrub superseded control-plane workflows
```

Commit hash:

```text
2a6b309594e10202b43f889e65223000a5ca99f9
```

## Concerns

- The exact inventory regex intentionally overmatches lockfiles, package URLs, and runtime fixture paths because `/packages` and `/deployments` are broad literals; I treated those as false positives and scrubbed only the supported docs/contracts/artifact surface.
- The generated Fleet bootstrap writer is still documented as a clean-slate implementation contract rather than a newly implemented mechanism in this docs-only task. The docs now explicitly mark manual `agent.toml` editing unsupported without inventing a concrete new bootstrap command syntax.
