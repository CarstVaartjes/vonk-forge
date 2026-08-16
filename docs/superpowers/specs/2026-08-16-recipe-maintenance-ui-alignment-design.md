# Recipe-maintenance UI alignment

## Intent

The v1 control experience has two user-facing recipe surfaces: `Library` for
operational truth and `Catalog` for recipe authoring and lifecycle work. The
legacy Profiles, Models, repository-editor, and reconciliation-plan surfaces
must not compete with those workflows.

## Experience contract

- `Library` is the default place to understand model families, accepted recipe
  revisions, cluster placement, runtime state, and available activation or
  recovery actions.
- `Catalog` is the maintenance workspace. It supports creating a local recipe,
  importing a WorkloadRun, inspecting a revision, editing drafts, resolving an
  immutable revision, attaching build evidence, and mapping it to a cluster.
- The visual recipe editor is the primary authoring UI. Canonical JSON remains
  available as an explicitly labelled advanced section for custom recipes and
  debugging; it is not a second authority.
- Recipe identity, revision, content digests, evidence, cluster mapping, and
  activation continue to come from the v1 control APIs. This work changes
  presentation and route ownership only; it does not create client-side
  authority or bypass server checks.

## Visual and interaction system

Retain the redesigned control-plane visual language already used by Fleet and
Library: dark green surfaces, mint authority accents, compact status pills,
responsive cards, clear loading/error/empty states, visible freshness, and
keyboard-visible focus. The sidebar exposes Fleet and Library directly, groups
operational activity separately from system administration, and does not expose
legacy Profiles or Models links.

Recipe lifecycle actions must have explicit labels and state feedback. A
resolved revision is visibly immutable; evidence and mapping actions explain
what is required before publication or activation. Mobile layouts must remain
usable without horizontal scrolling, and advanced technical details should be
progressively disclosed.

## Removal boundary

Remove old profile/model pages, repository editing, and profile reconciliation
routes and their generated client surface where they are no longer part of the
v1 contract. Retain shared proposal, audit, package, deployment, update, fleet,
agent, and recipe-route APIs that are still consumed by v1 workflows.

## Verification

The control web suite must cover navigation, Library grouping, catalog/editor
lifecycle states, advanced-document disclosure, responsive-safe rendering, and
absence of legacy navigation/routes. The backend OpenAPI document and generated
TypeScript client must be regenerated and checked against the retained API.
