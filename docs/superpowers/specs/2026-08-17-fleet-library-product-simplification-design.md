# Fleet and Library product simplification

## Problem

The current control plane exposes backend concepts as peer pages: Agents,
Catalog, Packages, Deployments, Updates, and Jobs. The result is a fragmented
operator experience with duplicated lifecycle concepts and jargon such as
“workload,” “package,” and “deployment.”

Spark membership is also split between two authorities. The current Fleet
projection reads the Git-controlled `inventory/fleet.toml` node roster first and
then joins PostgreSQL registration, certificate, telemetry, and inventory
state. That makes repository-only definitions appear as Sparks and can hide a
real enrollment whose ID is not in the Git roster.

The product has two natural user tasks:

1. Manage the Sparks that make up the cluster.
2. Manage the models and services that run on those Sparks.

Those tasks should be completed from Fleet and Library respectively. Internal
workflow objects and safety gates should remain implementation details unless
they are needed to explain an action or its progress.

## Approved information architecture

The primary application exposes only:

- **Fleet** — Spark health, identity, capacity, platform software, telemetry,
  and Spark-specific actions.
- **Library** — model/service discovery, recipe creation and import, readiness,
  placement, installation, running, and lifecycle actions.

The authenticated user menu contains:

- **Settings** — operator/environment settings that are actually supported.
- **Audit log** — a compact history of administrative actions.
- **Logout**.

The standalone Agents, Catalog, Packages, Deployments, Updates, and Jobs pages
are removed from the user-facing navigation and routes. Their components,
tests, and documentation are deleted or folded into Fleet/Library as described
below. There is no replacement “Advanced” section.

PostgreSQL is the management authority for the live Spark roster. The
repository is not required to add, remove, or display a Spark.

## Fleet experience

Fleet is the single Spark management surface.

### PostgreSQL-backed Spark set

`AgentNode` is the root set for Fleet. The live Fleet stream and HTTP fleet
evidence response query PostgreSQL registrations first, then join certificates,
presence, telemetry, inventory, installed services, and reservations.

- A fresh/reset database with no non-revoked enrolled `AgentNode` records
  renders zero Sparks.
- Each successfully enrolled, non-revoked Spark appears exactly once,
  regardless of whether a Git repository record exists.
- Offline or stale enrolled Sparks remain visible with truthful status and
  timestamps.
- Pending enrollment evidence is shown as an enrollment state in the Fleet
  enrollment flow, not as an active Spark card. Revoked identities remain
  available through the audit trail and identity history.
- `inventory/fleet.toml` is not consulted to decide Fleet membership and is not
  required for enrollment.

### Spark metadata

Mutable operator metadata belongs in PostgreSQL alongside the registration.
Implement a one-to-one profile owned by `AgentNode` (or equivalent columns in
that model) containing the display name, optional hostname, lifecycle label, and
bounded labels. Until a profile is supplied, Fleet uses these safe defaults:

- display name: immutable node ID;
- hostname: empty/unreported;
- lifecycle: `managed`;
- labels: empty.

Hardware facts and capabilities continue to come from the authenticated
`NodeInventorySnapshot`; connection and certificate facts continue to come from
the existing PostgreSQL registration tables. Library placement uses this
PostgreSQL-backed Fleet set.

### Spark card and detail actions

Each Spark card/detail view presents friendly status rather than raw lifecycle
terminology:

- Connected, delayed, offline, enrollment pending, certificate issue, or
  update available.
- Last contact time and telemetry freshness.
- Platform/agent version and update readiness.
- GPU, memory, disk, and inventory summary.
- Models/services currently installed or running.

Contextual actions include only operations supported by the existing secure
interfaces:

- **Add Spark** — visible as a small, accessible `+` control in the Fleet
  header, including the empty state.
- **Enroll/review Spark** — create a short-lived grant, show the secure pairing
  steps, and review evidence in the same Fleet context.
- **Update Spark** — preview and apply the exact signed platform update plan,
  with canary, progress, recovery approval, and rollback shown inline.
- **Manage identity** — inspect certificate state and perform explicit,
  confirmed revocation. A revoked identity is not promised an automatic
  same-node re-enrollment because the current enrollment protocol deliberately
  rejects a new enrollment while the old `AgentNode` row remains revoked.
  Replacement hardware or a lost identity uses a new, explicitly approved
  enrollment identity until a separate identity-reset protocol is designed.
- **View details** — telemetry, capacity, inventory, and relevant audit links.

The browser never SSHes to a Spark, writes its filesystem, or exposes a
long-lived credential. The Spark agent continues to generate keys/CSRs and
evidence, collect issued certificates, and run its normal authenticated loop.

## Library experience

Library is the single model/service lifecycle surface.

### Unified lifecycle

A user can complete the following without leaving Library:

1. Browse accepted models and service definitions.
2. Create a recipe or import one from the public catalog.
3. Review the exact version, source, resource needs, and topology.
4. See whether the required package/build is ready, pending, rejected, or
   needs review.
5. Select one or more eligible Sparks.
6. Review placement, capacity, ranks, and safety evidence.
7. Install, load/run, stop, uninstall, or remove the selected model/service.
8. See operation progress and any operator decision inline.

User-facing copy uses “model,” “service,” “version,” “Install,” and “Run.”
Backend entities such as recipes, package candidates, deployments, jobs, and
operations may appear as concise status details only when they help the user
understand an action.

### Advanced detail without advanced pages

Recipe editing, public import, source review, placement mapping, package
validation, rollout preview, rollback, and operation progress become nested
Library views or dialogs. They retain exact-digest confirmation and server
authority previews where safety requires them.

Long-running jobs are represented by inline progress and actionable recovery
states. A user does not need to open a separate Jobs page to discover whether
an install or run is still progressing.

## Audit and settings

Audit is removed as a primary page and exposed as a compact user-menu item.
Fleet and Library actions can link to filtered audit entries when useful. The
audit endpoint and storage remain only if the user menu or those contextual
links consume them.

Settings is a small user/admin surface. It must contain real supported settings
only; an empty placeholder page is not created merely to satisfy navigation.

## Removal and dependency policy

This is a functional simplification, not a cosmetic menu change.

1. Inventory every current page, route, API method, backend operation,
   component, test, and documentation reference.
2. Identify the Fleet or Library user journey that consumes each item.
3. Keep and refactor items required by those journeys, including security
   gates, exact previews, validation, rollback, certificate handling, and
   durable progress.
4. Delete items with no remaining consumer: standalone page components,
   routes, page-only API wrappers, dead schemas, tests, fixtures, and docs.
5. Do not retain a page or API solely because it existed previously.
6. Do not delete backend safety mechanics merely because their old page is
   gone; they must be callable from Fleet/Library or removed when genuinely
   unused.
7. Remove `inventory/fleet.toml` as a Spark-roster dependency. Update or delete
   node-onboarding instructions, repository fixtures, proposal contracts, and
   tests that exist only to maintain that roster. Retain the file only if the
   dependency audit proves it still serves a separate, non-roster policy.
8. Rewrite the existing Catalog maintenance design and operator runbooks so
   they describe the Library workflow. No documentation may direct an
   operator to a removed Catalog, Packages, Deployments, Updates, Jobs, or
   `inventory/fleet.toml` workflow.

The old endpoints may be retained temporarily during the migration only when
they are actively used by Fleet/Library. Once replacement consumers are in
place, unused endpoint groups and their tests are removed as part of the same
change.

## Acceptance criteria

- The primary navigation contains Fleet and Library only.
- Settings, Audit log, and Logout are available from the authenticated user
  menu; Audit is not a primary page.
- No standalone Agents, Catalog, Packages, Deployments, Updates, or Jobs
  pages/routes remain in the user-facing application.
- A reset database shows zero Spark cards and no unregistered placeholder
  cards.
- Each enrolled Spark appears once and exposes status plus relevant actions.
- An enrolled Spark absent from any repository document still appears with the
  PostgreSQL profile or the documented safe metadata defaults.
- Fleet can start the secure Add Spark/enrollment flow and manage platform
  update status without leaving Fleet.
- Library can create/import a recipe, review readiness, select Sparks, install,
  run, stop, remove, and follow progress without leaving Library.
- Exact previews, digest confirmations, certificate controls, and operator
  recovery gates remain intact.
- Fleet does not advertise same-identity re-enrollment after revocation unless
  a separately implemented identity-reset protocol is present.
- Every retained API/module has a Fleet or Library consumer, and every removed
  API/module has no remaining consumer proven by tests and repository search.
- Repository node-roster documentation and contracts are removed or rewritten
  consistently with PostgreSQL-backed Spark management.
- Automated tests cover the new unified journeys and fail if the removed pages
  or placeholder concepts return.
