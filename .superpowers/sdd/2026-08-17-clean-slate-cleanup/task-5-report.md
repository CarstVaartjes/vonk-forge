# Task 5 report — scrub legacy documentation, repository contracts, and artifacts

Date: Monday, August 17, 2026
Branch: `refactor/clean-slate-control-plane`

## Scope completed

- Rewrote the reviewer-flagged supported runbooks to remove Catalog as an operator surface and keep Fleet/Library as the only supported product surfaces.
- Rewrote the install/onboarding/development docs to stop advertising a usable generated bootstrap command before an emitter exists.
- Documented the supported registration boundary instead: registration is the authority, manual `agent.toml` editing is unsupported, and Fleet **Add Spark** is the next implementation step rather than an operator command currently available.
- Updated the documentation contract tests first, ran them red, then rewrote the docs to green.
- Deleted one extra stale supported runbook that the follow-up sweep exposed: `docs/runbooks/global-catalog.md`.

## Reference inventory

Exact brief inventory command run:

```bash
rg -n "inventory/fleet\.toml|config/package-families|config/workload-deployments|/packages|/deployments|Catalog page|Packages page|Deployments page|Jobs page|edit .*agent\.toml" README.md docs control agent scripts tests packaging
```

Inventory result notes:

- The command produced a broad result set because `/packages` also matched legitimate apt-package URLs, package/runtime fixtures, and lockfiles.
- It also intentionally matched preserved active cleanup/spec material that describes the removal itself.
- Relevant post-fix observations from that exact inventory:
  - supported operator docs no longer expose Catalog/Packages/Deployments page workflows;
  - legitimate remaining `/packages` hits are package-release URLs and non-doc package/runtime fixtures;
  - preserved active clean-slate design/plan files still mention removed workflows only as removal targets, which is expected.

Focused supported-doc/test sweeps run after the rewrite:

Catalog surface sweep:

```bash
rg -n '(`/catalog`|`Catalog`| Catalog )' docs/README.md docs/runbooks docs/operations docs/operators tests/test_docs_contract.py tests/runbooks
```

Output:

```text
tests/test_docs_contract.py:918:        " Catalog ",
tests/test_docs_contract.py:919:        "`Catalog`",
```

Docs-only Catalog surface sweep:

```bash
rg -n '(`/catalog`|`Catalog`| Catalog )' docs/README.md docs/runbooks docs/operations docs/operators
```

Output:

```text
[no output]
```

Removed Packages/Deployments/Updates/Jobs page/route sweep:

```bash
rg -n '(`/packages`|`/deployments`|`/updates`|`/jobs`|`Packages`|`Deployments`|`Updates`|`Jobs`)' docs/README.md docs/runbooks docs/operations docs/operators tests/test_docs_contract.py tests/runbooks
```

Output:

```text
[no output]
```

Git roster sweep:

```bash
rg -n '(Git Spark roster|git spark roster|Git roster|git roster)' docs/README.md docs/runbooks docs/operations docs/operators tests/test_docs_contract.py tests/runbooks
```

Output:

```text
[no output]
```

Manual `agent.toml` editing sweep:

```bash
rg -n -i '(manual `agent.toml` editing|sudoedit /etc/vonk-forge-agent/agent.toml|/etc/vonk-forge-agent/agent.toml)' docs/README.md docs/runbooks docs/operations docs/operators tests/test_docs_contract.py tests/runbooks
```

Output:

```text
tests/runbooks/test_node_onboarding.py:19:        "manual `agent.toml` editing is unsupported",
tests/runbooks/test_agent_installation.py:18:        "manual `agent.toml` editing is unsupported",
tests/runbooks/test_agent_installation.py:21:    assert "sudoedit /etc/vonk-forge-agent/agent.toml" not in text
tests/runbooks/test_agent_installation.py:33:        "manual `agent.toml` editing is unsupported",
tests/runbooks/test_agent_installation.py:37:    assert "/etc/vonk-forge-agent/agent.toml" not in text
tests/runbooks/test_agent_installation.py:49:        "manual `agent.toml` editing is unsupported",
tests/runbooks/test_agent_installation.py:52:    assert "/etc/vonk-forge-agent/agent.toml" not in text
tests/runbooks/test_development_nas_installation.py:273:        "manual `agent.toml` editing is unsupported",
docs/operations/install-vonk-agent.md:119:local configuration checklist. Manual `agent.toml` editing is unsupported.
docs/runbooks/development-agent-workloads.md:375:operator command currently available. Manual `agent.toml` editing is
docs/runbooks/fresh-development-install.md:271:   inputs for that Spark. Manual `agent.toml` editing is unsupported, and the
docs/runbooks/node-onboarding.md:86:1. Registration is the authority: manual `agent.toml` editing is unsupported,
```

Interpretation: supported docs now retain only explicit prohibitions against manual `agent.toml` editing; the old editable-file workflow is gone.

## Changed files

- Modified: `docs/README.md`
- Modified: `docs/operations/install-vonk-agent.md`
- Modified: `docs/operators/execution-harnesses.md`
- Modified: `docs/operators/model-catalog.md`
- Modified: `docs/operators/recipe-library.md`
- Modified: `docs/runbooks/development-agent-workloads.md`
- Modified: `docs/runbooks/fresh-development-install.md`
- Modified: `docs/runbooks/hermes-agent.md`
- Modified: `docs/runbooks/model-switching.md`
- Modified: `docs/runbooks/node-onboarding.md`
- Modified: `docs/runbooks/platform-update.md`
- Modified: `docs/runbooks/repository-administration.md`
- Modified: `docs/runbooks/runtime-release.md`
- Modified: `docs/runbooks/vonkctl.md`
- Modified: `tests/runbooks/test_agent_installation.py`
- Modified: `tests/runbooks/test_development_nas_installation.py`
- Modified: `tests/runbooks/test_node_onboarding.py`
- Modified: `tests/test_docs_contract.py`
- Modified: `.superpowers/sdd/2026-08-17-clean-slate-cleanup/task-5-report.md`

## Deleted files

- Deleted: `docs/runbooks/global-catalog.md`

## TDD evidence

Tests/assertions updated before doc changes:

- `tests/test_docs_contract.py`
- `tests/runbooks/test_agent_installation.py`
- `tests/runbooks/test_development_nas_installation.py`
- `tests/runbooks/test_node_onboarding.py`

Focused red run executed before the doc rewrite:

```bash
uv run --project control --with-editable . --frozen python -m pytest -q tests/test_docs_contract.py tests/runbooks/test_agent_installation.py tests/runbooks/test_development_nas_installation.py tests/runbooks/test_node_onboarding.py
```

Red result summary:

- 11 failures, including the reviewer-flagged Catalog/operator-surface assertions and the bootstrap-boundary assertions.

## Verification commands and exact output

Focused documentation contract/runbook rerun:

```bash
uv run --project control --with-editable . --frozen python -m pytest -q tests/test_docs_contract.py tests/runbooks/test_agent_installation.py tests/runbooks/test_development_nas_installation.py tests/runbooks/test_node_onboarding.py
```

Output:

```text
...................................................................      [100%]
67 passed in 0.05s
```

Required runbook/repository/proposal suite rerun:

```bash
uv run --project control --with-editable . --frozen python -m pytest -q tests/runbooks control/tests/test_repository.py control/tests/test_proposals.py
```

Output:

```text
55 passed, 1 skipped in 2.82s
```

Whitespace/diff hygiene:

```bash
git diff --check
```

Output:

```text
[no output]
```

## Commit

Commit message required by brief:

```text
docs: scrub superseded control-plane workflows
```

Commit hash:

```text
3fa9a45d2148653a671f37e392cffb2ece632fc6
```

## Concerns

- The exact brief inventory regex is intentionally noisy because `/packages` matches legitimate package URLs and package-related fixtures outside the supported operator-doc surface. The narrower post-fix sweeps are the authoritative proof that supported docs/tests no longer present removed Catalog/Packages/Deployments/Updates/Jobs/Git-roster workflows.
- This was a docs/contracts scrub only. The Fleet **Add Spark** bootstrap emitter still does not exist, so the docs now accurately stop at the registration boundary and describe the bootstrap action as the next implementation step rather than a currently usable operator command.
