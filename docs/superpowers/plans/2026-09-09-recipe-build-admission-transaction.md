# Recipe build admission transaction boundary

## Problem

Availability builder selection currently locks the availability parent job and
the selected `AgentNode`, then calls `RecipeBuildService.plan`. `plan` opens a
second transaction and inserts a `RecipeBuild` referencing that node. On
PostgreSQL the insert waits for the outer transaction's row lock while the
outer worker waits for `plan`, leaving the Controller pool blocked.

## Design

`RecipeBuildService` will split build planning into two phases:

1. `prepare_plan` resolves and validates the recipe, verified source bundle,
   Dockerfile policy, fresh inventory, and builder identity without writing
   database state. Source reads, policy checks, inventory work, and all other
   potentially slow operations happen before admission locks are acquired.
2. `persist_plan_in_session` performs the existing idempotent `RecipeBuild`
   lookup/insert using a caller-owned SQLAlchemy session. It rechecks the
   builder row while locked so a changed builder identity cannot accept a
   stale prepared plan. The public `plan` method remains a short wrapper that
   prepares first and then calls this helper in one short transaction.

Availability selection prepares a candidate outside locks. It then opens one
   short transaction, locks the parent job and candidate node in stable order,
   rechecks current work, persists the plan through the shared session helper,
   and updates the parent runtime identity before commit. If another worker
   wins the candidate between preparation and commit, the current-work check
   selects another candidate and prepares again. No source download, inventory
   read, or build/network operation is performed while row locks are held.

The parent lock serializes assignment for one availability operation; the
candidate lock and fresh work recheck preserve concurrent builder selection.
The helper keeps the existing recipe/build identity reuse and force behavior.
If preparation fails, no `RecipeBuild` or resource reservation is created. If
session persistence fails, the transaction rolls back both the build row and
the parent assignment, so there is no orphan admission reservation. Later
`reserve_in_session` continues to create resource reservations only in the
transaction that queues the durable build operation.

## Verification

PostgreSQL tests will run two concurrent availability builders with a bounded
barrier and prove they finish without the cross-session foreign-key lock, pick
distinct eligible nodes, converge on repeated identity, and leave no build or
resource reservation after a prepared-plan persistence failure.
