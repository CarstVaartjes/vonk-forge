# Test Git-state audit

Scope: the owner's `git grep -lnE '"git"|origin/main|rev-parse|git show'`
over `tests/*.py`, `tests/**/*.py` and `control/tests/*.py`, plus test helpers.
The initial search found 21 files: seven with real-checkout subprocesses,
five with owned temporary repositories, and nine with data/assertions or fake
executables rather than real Git-state reads. Counts below count static Git
command sites, not executions of parametrized tests.

| File | Initial classification and disposition |
| --- | --- |
| `control/tests/principle_guards.py` | real-repo-state: `show origin/main` and `rev-parse`; historical comparisons moved to `scripts/check-principle-history`, invoked by the CI Ruff job against the explicit fetched base. Current-tree allowlist checks and synthetic history-gate tests remain hermetic. |
| `control/tests/test_fresh_launch_catalog_postgres_acceptance.py` | real-repo-state: recipe checkout `rev-parse`; removed the unused HEAD lookup. The canonical index supplies and validates its immutable `source_commit`. |
| `control/tests/test_profile_effect_consumer_contract_postgres.py` | real-repo-state: platform and recipes `rev-parse`; platform build identity is an explicit workflow input, recipe checkout verification stays in the existing workflow step. |
| `tests/acceptance/test_spark_lifecycle.py` | real-repo-state: `rev-parse` and two `diff` commands; removed checkout-state checks. The helper manifest already binds the requested source identity and hashes every relevant source input and the helper binary; candidate identity is checked separately. |
| `tests/cluster_profiles/test_cli_update.py` | real-repo-state: `diff HEAD` and `rev-parse`; consume explicit prior/current source identities. The hosted transition workflow verifies both checkouts before running the proof. |
| `tests/scripts/test_publication_dev_ancestry.py` | real-repo-state: `rev-list HEAD`, followed by publisher ancestry queries on the real checkout; create an owned temporary history and point the publisher's source location there. No history-dependent skip remains. |
| `tests/test_shell_destructive_guard.py` | real-repo-state: `ls-files`; discover shell files on disk, including files absent from a Git index. A regression fixture exercises discovery without any Git repository. |
| `tests/scripts/test_check_python_types.py` | own-temp-repo: hook probe initializes and stages files in `tmp_path`. |
| `tests/scripts/test_resolve_publication_producer.py` | own-temp-repo: all helper commands use a history initialized in `tmp_path`; additionally made every Git cwd explicit and bounded command duration. |
| `tests/scripts/test_select_ci_areas.py` | own-temp-repo: initialization, staging, commit and deleted-file diff all target `tmp_path`. |
| `tests/scripts/test_verify_release_tag_authority.py` | own-temp-repo: owned work/remote repositories, tags, pushes and fetch/authority script calls. |
| `tests/test_container_release_workflow.py` | own-temp-repo: reusable image fixture initializes its own repository; commit, revision and checkout commands target that fixture. Workflow Git text assertions do not execute Git. |
| `control/tests/security/test_agent_protocol.py` | no Git call: contract source field data. |
| `control/tests/subprocess_environment.py` | no Git-state call: locates the Git executable for explicit subprocess environments; remaining subprocesses create a venv/install a wheel. |
| `tests/install/test_curl_bootstraps.py` | no real Git call: fake tool lookup for bootstrap probes. |
| `tests/scripts/test_agent_repair_lifecycle.py` | no Git call: assertions about workflow-only shell harness source. |
| `tests/scripts/test_ci_ruff.py` | no real Git call: fixture-owned fake Git executable. |
| `tests/scripts/test_promote_accepted_channel.py` | no real Git call: fixture-owned fake Git executable. |
| `tests/scripts/test_retry_dependency_fetch.py` | no real Git call: fixture-owned fake fetch executable and rejected command data. |
| `tests/test_agent_release_workflow.py` | no Git call: workflow source assertions. |
| `tests/test_recipe_library_ci_receipt.py` | no Git call: receipt source field data. |

Before: 12 direct real-checkout command sites in seven Python files.
After: zero; six files now create their own temporary repositories (including
the new ancestry fixture). Indirect ancestry queries now use that fixture too.

`tests/test_git_hermeticity.py` scans both suites recursively, including helper
modules. Its regression cases reject root-derived paths, path/command aliases,
`git -C`, inherited cwd and acceptance runner wrappers, and accept temporary
histories and inert workflow text. This is a syntax guard, not a proof about
arbitrary dynamically constructed subprocess commands. There are no Python
suite exceptions. Its reasoned workflow-only inventory identifies the historical
CI ratchet and the two native systemd shell harnesses, whose package provenance
checks run only in workflow lanes.

Verification for this track excludes all test execution by owner instruction.
The coordinator runs tests through GitHub Actions and commits with hooks.
