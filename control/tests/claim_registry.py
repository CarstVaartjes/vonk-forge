"""Every durable claim in the Controller schema, and the owner it serves.

The rule: A claim never outlives its owner; a claim whose owner has ended is
released or adopted by the next reconcile or admission that meets it.

A claim is a row that reserves a scarce or unique resource (a port, bytes, a
singleton slot, a node's mutation turn, a deletion fence) or blocks other work
while it is held. A claim whose owner ended without releasing it (a cancel, a
failure, a crash, a supersession, a deleted owner) must not block a fresh
request for that resource forever.

``CLAIMS`` names each claim table with its owner, the production predicate
that decides the owner has ended, and the production path that releases or
adopts it. ``NOT_CLAIMS`` gives the reason each claim-shaped table is not one.
``claim_shaped_tables`` is the net: a table whose name or columns read as a
claim, or whose unique key includes a node, must be in one of the two, so a new
claim table cannot skip the rule. The behaviour lives in
``test_claims_never_outlive_owners_postgres``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import MetaData, Table, UniqueConstraint


@dataclass(frozen=True, slots=True)
class Claim:
    owner: str
    ended: str
    reclaimed_by: str


CLAIMS: dict[str, Claim] = {
    "resource_reservations": Claim(
        owner="owner_kind/owner_id: a profile application, run, installation or build",
        ended="reservation_owners.reservation_owner(...).dead; a build that stopped "
        "is dead once its own Spark orders ended, and its memory claim counts "
        "only while it builds (memory_reservations)",
        reclaimed_by="AttemptResidueReconciler.tick and FleetProfileService.tick "
        "(release_dead_owner_reservations); a fresh load supersedes a live "
        "predecessor's promise",
    ),
    "artifact_lifecycle_gates": Claim(
        owner="removal_owner_kind/removal_owner_id: a model-cache operation or "
        "a recipe-image Job",
        ended="artifact_lifecycle.dead_removal_identities",
        reclaimed_by="ModelCacheService.reconcile_removal_gates and the image "
        "availability reconcile_removal_gates (worker ticks)",
    ),
    "recipe_library_sync_runs": Claim(
        owner="the running sync itself (heartbeat_at, else started_at)",
        ended="ManagedRecipeCatalogSyncService._expired (10-minute lease)",
        reclaimed_by="the next ManagedRecipeCatalogSyncService.sync admission",
    ),
    "agent_operation_attempts": Claim(
        owner="the agent order's current attempt (fence, lease_deadline)",
        ended="agent_operation_facts.attempt_is_live",
        reclaimed_by="AgentJobService.reconcile_orders (worker) and the claim "
        "of the next mutating order on that node",
    ),
    "artifact_distribution_assignments": Claim(
        owner="the active owner Job whose plan digest names the grant",
        ended="no active owner Job holds the plan (AttemptResidueReconciler)",
        reclaimed_by="AttemptResidueReconciler.tick, and register() replaces "
        "an inactive or expired grant",
    ),
    "agent_certificate_rotations": Claim(
        owner="the node's renewal request (source serial, CSR key)",
        ended="the renewing process died: its intent is unbound or past the "
        "issuance window",
        reclaimed_by="the recovery of the next renewal (EnrollmentService.renew); "
        "reconcile_revocations sweeps rows past the window",
    ),
    "recipe_builds": Claim(
        owner="the recipe.build Job whose payload owner_id is the build",
        ended="that Job has ended or is missing",
        reclaimed_by="RecipeOperationService.build reopens a failed or planned "
        "build; nothing reopens a building row whose Job ended",
    ),
}


NOT_CLAIMS: dict[str, str] = {
    "agent_nodes": "the node identity itself",
    "agent_certificates": "issued credentials of a node; identity, not a reservation",
    "agent_presence": "the latest observation of a node",
    "agent_node_profiles": "the observed hardware of a node",
    "artifact_job_files": "a named file slot inside its own artifact job",
    "cluster_mappings": "an immutable placement; endpoint_owner names a rank",
    "cluster_mapping_nodes": "the ranks of an immutable placement",
    "installation_nodes": "an installation's members; its disk claims are "
    "resource_reservations",
    "run_nodes": "a run's members; its port and memory claims are "
    "resource_reservations",
    "node_artifacts": "observed artifacts on a node",
    "node_inventory_snapshots": "observed inventory of a node",
    "node_telemetry_latest": "observed telemetry of a node",
    "node_telemetry_samples": "observed telemetry of a node",
    "route_publication_owner": "a transaction-scoped lock anchor; its durable "
    "columns record the last activation, and a newer claim supersedes",
    "job_attempts": "an execution lease on its own Job; it reserves nothing "
    "another request needs, and the next claim lapses it",
    "model_cache_operations": "an execution lease on its own operation; it "
    "reserves nothing another request needs, and the next claim lapses it",
}


_CLAIM_NAME = re.compile(r"reserv|promis|lease|hold|claim|lock|gate|slot|rotation")
_CLAIM_COLUMN = re.compile(
    r"(^|_)(lease|fence|slot|holder|owner|port|resource_key)(_|$)"
)
_NODE = re.compile(r"(^|_)node_id$")


def _unique_keys(table: Table) -> Iterator[set[str]]:
    yield {column.name for column in table.primary_key.columns}
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            yield {column.name for column in constraint.columns}
    for index in table.indexes:
        if index.unique:
            yield {column.name for column in index.columns}


def claim_shaped_tables(metadata: MetaData) -> set[str]:
    """Tables that look like a claim: by name, by column, or a unique node key."""

    return {
        table.name
        for table in metadata.tables.values()
        if _CLAIM_NAME.search(table.name)
        or any(_CLAIM_COLUMN.search(column.name) for column in table.columns)
        or any(any(_NODE.search(name) for name in key) for key in _unique_keys(table))
    }
