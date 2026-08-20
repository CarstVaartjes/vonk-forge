# PostgreSQL Runtime Authority Design

## Goal

Make the control plane independent of a host Git checkout. All persistent
runtime authority, configuration revisions, proposals, and audit data live in
PostgreSQL. GitHub remains the source for source code and published releases;
the NAS receives only Compose configuration, secrets, images, and Docker
volumes.

## Design

Add a PostgreSQL-backed authority store containing immutable authority revisions
and a singleton current-head row. Each revision stores the allowlisted control
documents currently represented by the runtime checkout, including the default
topology document. Proposal previews are persisted in PostgreSQL and are
applied with a locked compare-and-swap against the current head. The resulting
revision identifier continues to travel through reconciliation and worker
authority messages, but it is a database revision rather than a Git commit.

This is a fresh deployment model; no Git-shaped identifier or compatibility
layer is required. Reconciliation and worker authority messages use opaque
database revision IDs. Change submission creates a database revision with no
branch or pull request. Git signing keys, Git policy files, repository paths,
and Git-specific runtime services are removed from production startup and
Compose.

The database authority store is used by dashboard and fleet projections,
reconciliation authority, update topology selection, and worker authority.
Existing PostgreSQL recipe/catalog tables remain the source for recipe data;
the authority document store is only for the remaining platform authority
documents until those are represented by their dedicated relational models.

## Persistence boundary

All durable control-plane records and bounded content belong in PostgreSQL:
authority documents, proposals, catalog records, source bundles, job logs,
fleet state, and operation evidence. Large OCI/image layer bytes are blob
transport rather than authority; they use the dedicated registry or an
ephemeral transfer staging area and are never used as the source of truth.
Runtime secrets, private keys, and cryptographic publication material remain
Docker secrets or purpose-specific volumes because they are credentials/material,
not application state. Temporary files and socket/route exchange paths remain
ephemeral.

## Acceptance criteria

1. Production control-api starts without `REPOSITORY_PATH`, `/repository`, a
   Git signing key, or a host filesystem checkout.
2. A fresh PostgreSQL database receives an initial authority head and default
   topology document through the normal schema/bootstrap path.
3. Proposal preview and submission survive an API restart because their state
   is persisted in PostgreSQL.
4. Stale proposal bases are rejected using a database compare-and-swap.
5. Dashboard, reconciliation, and worker authority use opaque database
   revisions and never invoke Git or expose Git commit terminology.
6. Compose has no repository bind mount, repository GID, Git signing secret,
   or repository-related environment variable.
7. Tests cover database authority behavior and assert the production Compose
   graph has no host repository dependency.
