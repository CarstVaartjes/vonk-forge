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
4. See whether the selected model/service version is ready to install, still
   building, blocked, or needs review.
5. Select one or more eligible Sparks.
6. Review placement, capacity, ranks, and safety evidence.
7. Install, load/run, stop, uninstall, or remove the selected model/service.
8. See operation progress and any operator decision inline.

User-facing copy uses “model,” “service,” “version,” “Install,” and “Run.”
The new Library implementation owns its own PostgreSQL-backed version,
artifact, placement, installation, and run records. It does not expose or
depend on the old package-candidate, package-deployment, or job pipeline.
Internal operation state may appear as concise progress details only when it
helps the user understand an action.

### Advanced detail without advanced pages

Recipe editing, public import, source review, placement mapping, artifact
readiness, install preview, stop/remove confirmation, and operation progress
become nested Library views or dialogs. They retain exact-digest confirmation
and server-authority previews where safety requires them.

Long-running operations are represented by inline progress and actionable
recovery states. A user does not need to open a separate Jobs page to discover
whether an install or run is still progressing.

## Spark bootstrap and registration

The Spark package installation and registration flow is also clean-slate and
generated. The operator does not manually edit an agent TOML file.

1. The operator clicks **Add Spark** in Fleet.
2. Fleet creates a short-lived, node-bound bootstrap grant and registration
   intent in PostgreSQL.
3. Fleet provides a one-time bootstrap command or protected bootstrap file for
   the Spark. It contains the non-secret enrollment/controller endpoints,
   public CA trust material or fingerprint, the assigned node identity, and
   safe runtime defaults. The one-time token is the only secret.
4. The operator runs the bootstrap once on the Spark after installing the
   signed Spark-agent installer.
5. The agent writes its local runtime configuration, generates its key and CSR,
   submits hardware/host/agent/boot evidence, and waits for approval.
6. After approval, the agent collects the certificate, removes the consumed
   token, and starts its authenticated runtime loop.

The browser never SSHes to a Spark or writes its filesystem. The installation
command is the only host-side action; it is generated from the Fleet
registration state and must not require hand-editing `/etc/vonk-forge-agent/
agent.toml`.

## Audit and settings

Audit is removed as a primary page and exposed as a compact user-menu item.
Fleet and Library actions can link to filtered audit entries when useful. The
audit endpoint and storage remain only if the user menu or those contextual
links consume them.

Settings is a small user/admin surface. It must contain real supported settings
only; an empty placeholder page is not created merely to satisfy navigation.

## Clean-slate removal policy

This is a direct replacement, not a migration. The old implementation is not
kept behind the new UX.

1. Inventory every current page, route, API method, backend operation,
   component, test, and documentation reference.
2. Identify the Fleet or Library user journey that consumes each item.
3. Implement only the new Fleet/Library data models, routes, and workflows,
   including security gates, exact previews, validation, certificate handling,
   and durable progress where the new UX needs them.
4. Delete the old Agents, Catalog, Packages, Deployments, Updates, and Jobs
   pages, routes, API groups, schemas, services, modules, fixtures, tests, and
   documentation. Do not wrap or alias them into the new implementation.
5. Delete the old package/deployment pipeline directly, including package
   candidate promotion, deployment rollout/rollback artifacts, legacy package
   and deployment TOML documents, and their dedicated orchestration code.
6. Delete `inventory/fleet.toml` and all Spark-roster proposal, loader,
   projection, onboarding, and documentation dependencies. PostgreSQL is the
   only live Spark-management authority.
7. Do not add migration code, compatibility shims, redirect routes, legacy
   schema readers, data translators, dual-write paths, or old API aliases.
   Existing development state is disposable and must be recreated under the
   new schema and workflows.
8. Rewrite or delete every design document, runbook, fixture, and test that
   describes the removed Catalog, package/deployment pipeline, Jobs page, or
   Git-managed Spark roster. Documentation must describe only the new Fleet,
   Library, and generated bootstrap flow.
9. Keep a TOML file only when it is a toolchain/project manifest or a generated
   local runtime file that the new system genuinely requires. File format
   alone is not a reason to retain legacy behavior.

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
- Add Spark generates the host bootstrap inputs and never requires manual
  editing of the agent runtime TOML.
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
- No old package/deployment API, TOML artifact, migration adapter, compatibility
  route, or legacy page remains in the supported source tree.
- Automated tests cover the new unified journeys and fail if the removed pages
  or placeholder concepts return.
