# Model and Recipe authority

The separate [`vonk-forge-recipes`](https://github.com/CarstVaartjes/vonk-forge-recipes)
repository is the sole authority for ModelDefinition and RecipeDefinition
documents, their immutable package inputs, and any discovery metadata. The
platform repository does not mirror a model-target ledger or authored recipe
files.

The Controller resolves the reviewed library branch to one commit, validates
the schema-2 catalog index and package closure, and synchronizes that snapshot
through the managed catalog routes. Local PostgreSQL then owns imported
revisions, installation, placement, run state, and evidence. A model or recipe
becomes runnable only after its exact immutable definition passes the current
structural and physical acceptance gates.

For local checks, run `scripts/validate-recipe-library` against the sibling
checkout and qualify a selected document with `scripts/qualify-recipe`. Use the
Controller's managed catalog sync to refresh Library metadata; do not create or
edit a platform-local target or recipe ledger.
