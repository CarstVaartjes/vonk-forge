# Fresh launch catalog PostgreSQL acceptance

This evidence belongs to the isolated `codex/pg-acceptance-3943` checkout,
based on local integration commit
`1d4093fa12c4365755f9465195d70faeaf3019e0`.
The acceptance test is
`control/tests/test_fresh_launch_catalog_postgres_acceptance.py`.

The lane consumes either a verified publication receipt named by
`VONK_CATALOG_CORPUS_EVIDENCE` or the frozen contracts checkout named by
`VONK_FROZEN_CONTRACTS_ROOT`. It reads the complete schema-2 index and package
archives from that input, derives Model and Recipe identities and counts from
the index, and requires the package closure to preserve Models that no Recipe
selects. The PostgreSQL test upgrades a newly created database in OrbStack,
imports the complete corpus, checks the canonical active revisions and typed
Model/Recipe Library responses, and checks that legacy tables, readers, and
authoring routes are absent from the active surface.

The test verifies that a repository input's supplied frozen-contracts commit
matches that checkout's immutable Git HEAD (or binds a cached input to its
receipt identity), and does not contain fixed catalog counts. The connected run
below used exact candidate commit
`fcf601339bc726af5f1a41f5abe1e331ccf32af4`. Its index-derived
corpus contained 92 Models and 85 Recipes; 13 Models had no Recipe selection,
and all of those unlinked Models remained in the typed Library projection. The
schema-2 typed Library list responses embedded the shared Model and Recipe
documents, cursor pagination covered the complete 92-Model and 85-Recipe
sets, and Recipe detail returned every selected Model document in source order
for a multi-Model Recipe.
The P5 production Run path remains a separate pending boundary and is not
represented by this catalog acceptance.

Validation command:

```text
PYTHONPATH=/private/tmp/vonk-public-contracts-source-fcf/src:control/src \
VONK_FROZEN_CONTRACTS_ROOT=/private/tmp/vonk-forge-recipes-qwen38-vllm-main57 \
VONK_FROZEN_CONTRACTS_COMMIT=fcf601339bc726af5f1a41f5abe1e331ccf32af4 \
/opt/vonk-forge/control/.venv/bin/pytest \
  --basetemp=/private/tmp/vonk-pg-run-3943-rebased -q -s \
  control/tests/test_fresh_launch_catalog_postgres_acceptance.py::test_fresh_orbstack_postgres_imports_typed_canonical_model_recipe_api
```

Result: `1 passed in 10.14s` against Docker context `orbstack`. The disposable
PostgreSQL database and the 264 MiB pytest temporary output were cleaned after
the run. The check also verified the canonical active revision counts, package
closure, typed schema-2 Library Model/Recipe list/detail responses with shared
documents, complete cursor pagination, ordered multi-Model detail selection,
unlinked Model retention, and absence of legacy tables and authoring operation
IDs.

The focused lane requires the repository-pinned Ruff version and a healthy
OrbStack Docker context for its connected test. It never creates or removes
shared PostgreSQL volumes, modifies network/security settings, or changes
publication and dependency sources.
