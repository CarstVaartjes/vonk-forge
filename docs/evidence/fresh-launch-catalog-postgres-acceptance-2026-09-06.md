# Fresh launch catalog PostgreSQL acceptance

This evidence belongs to the isolated `codex/pg-acceptance-current` checkout,
based on local integration commit `91f7bb7a6a58bb3cfa2ad76103a630229b3ca830`.
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

The test binds the frozen contracts input to exact commit
`fcf601339bc726af5f1a41f5abe1e331ccf32af4` and does not contain fixed catalog
counts. A publication receipt or that exact frozen candidate must be supplied
before the connected result is recorded; an absent input is an explicit skip.
The P5 production Run path remains a separate pending boundary and is not
represented by this catalog acceptance.

Validation command:

```text
UV_CACHE_DIR=/private/tmp/vonk-forge-pg-acceptance-uv-cache \
  uv run --project control --frozen --with-editable . pytest -q \
  control/tests/test_fresh_launch_catalog_postgres_acceptance.py
```

The focused lane requires the repository-pinned Ruff version and a healthy
OrbStack Docker context for its connected test. It never creates or removes
shared PostgreSQL volumes, modifies network/security settings, or changes
publication and dependency sources.
