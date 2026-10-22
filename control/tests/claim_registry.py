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

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import MetaData, Table, UniqueConstraint
from vonk_control.owner_liveness import OwnerKind

#: The one module that may read an owner's stored state as liveness.
OWNER_LIVENESS = "control/src/vonk_control/owner_liveness.py"


@dataclass(frozen=True, slots=True)
class Claim:
    #: The mapped class of the claim table.
    model: str
    owner: str
    #: The ``owner_liveness`` kinds whose ``owner()`` decides the owner ended.
    ended: tuple[OwnerKind, ...]
    reclaimed_by: str
    #: Stored states of a claim row that read as "its owner is still working";
    #: only ``owner_liveness`` may decide liveness from them.
    in_flight_states: tuple[str, ...] = ()


CLAIMS: dict[str, Claim] = {
    "resource_reservations": Claim(
        model="ResourceReservation",
        owner="owner_kind/owner_id: a profile application, run, installation or "
        "build (a build that stopped is dead once its own Spark orders ended)",
        ended=(
            OwnerKind.FLEET_PROFILE,
            OwnerKind.RUN,
            OwnerKind.INSTALLATION,
            OwnerKind.RECIPE_BUILD,
        ),
        reclaimed_by="AttemptResidueReconciler.tick and FleetProfileService.tick "
        "(release_dead_owner_reservations); a fresh load supersedes a live "
        "predecessor's promise",
    ),
    "artifact_lifecycle_gates": Claim(
        model="ArtifactLifecycleGate",
        owner="removal_owner_kind/removal_owner_id: a model-cache operation or "
        "a recipe-image Job",
        ended=(OwnerKind.CACHE_OPERATION, OwnerKind.IMAGE_JOB),
        reclaimed_by="ModelCacheService.reconcile_removal_gates and the image "
        "availability reconcile_removal_gates (worker ticks), through "
        "artifact_lifecycle.dead_removal_identities",
    ),
    "recipe_library_sync_runs": Claim(
        model="RecipeLibrarySyncRun",
        owner="the running sync itself (heartbeat_at, else started_at)",
        ended=(OwnerKind.SYNC_RUN,),
        reclaimed_by="the next ManagedRecipeCatalogSyncService.sync admission",
        in_flight_states=("running",),
    ),
    "agent_operation_attempts": Claim(
        model="AgentOperationAttempt",
        owner="the agent order's current attempt (fence, lease_deadline)",
        ended=(OwnerKind.AGENT_ATTEMPT,),
        reclaimed_by="AgentJobService.reconcile_orders (worker) and the claim "
        "of the next mutating order on that node",
        in_flight_states=("running",),
    ),
    "artifact_distribution_assignments": Claim(
        model="ArtifactDistributionAssignment",
        owner="the active owner Job whose plan digest names the grant",
        ended=(OwnerKind.JOB,),
        reclaimed_by="AttemptResidueReconciler.tick, and register() replaces "
        "an inactive or expired grant",
    ),
    "agent_certificate_rotations": Claim(
        model="AgentCertificateRotation",
        owner="the node's renewal: every renew request of the intent touches it",
        ended=(OwnerKind.RENEWAL_INTENT,),
        reclaimed_by="EnrollmentService.reconcile_revocations, and the next "
        "renewal of that node replaces an ended intent at once",
        in_flight_states=("issuing",),
    ),
    "recipe_builds": Claim(
        model="RecipeBuild",
        owner="a not-yet-ended Job that names the build: its recipe.build Job "
        "or a parent that recorded it",
        ended=(OwnerKind.RECIPE_BUILD,),
        reclaimed_by="RecipeOperationService.build reopens a failed or planned "
        "build and starts a fresh attempt for a building row whose Job ended",
        in_flight_states=("planned", "building"),
    ),
}


NOT_CLAIMS: dict[str, str] = {
    "agent_nodes": "the node identity itself",
    "agent_certificates": "issued credentials of a node; identity, not a reservation",
    "agent_node_profiles": "the observed hardware of a node",
    "artifact_job_files": "a named file slot inside its own artifact job",
    "cluster_mappings": "an immutable placement; endpoint_owner names a rank",
    "cluster_mapping_nodes": "the ranks of an immutable placement",
    "installation_nodes": "an installation's members; its disk claims are "
    "resource_reservations",
    "run_nodes": "a run's members; its port and memory claims are "
    "resource_reservations",
    "evidence_repeats": "attention evidence of a repeated failure; it never "
    "holds or blocks work and clears on the next success",
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


def _words(node: ast.expr, constants: dict[str, set[str]]) -> set[str]:
    """The state words a literal, a named constant or an enum member spells."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return {word for item in node.elts for word in _words(item, constants)}
    if isinstance(node, ast.Name):
        return constants.get(node.id, set())
    if isinstance(node, ast.Call) and len(node.args) == 1:
        return _words(node.args[0], constants)
    if isinstance(node, ast.Attribute):
        member = node.value if node.attr == "value" else node
        if isinstance(member, ast.Attribute):
            return {member.attr.lower().replace("_", "-")}
    return set()


def _model_state(node: ast.expr) -> str | None:
    """``Model`` for ``Model.state``."""

    if (
        isinstance(node, ast.Attribute)
        and node.attr == "state"
        and isinstance(node.value, ast.Name)
    ):
        return node.value.id
    return None


def in_flight_by_stored_state(source: str, *, path: str) -> list[int]:
    """Lines that decide "its owner is working" from a claim row's stored state.

    ``Model.state.in_(...)`` or ``Model.state in (...)`` on a claim table,
    naming a state the registry lists as in flight, reads one stored signal
    as liveness. Only ``owner_liveness`` may: everyone else asks ``owner()``
    or uses ``builds_in_flight``.
    """

    if path == OWNER_LIVENESS:
        return []
    in_flight = {
        claim.model: set(claim.in_flight_states)
        for claim in CLAIMS.values()
        if claim.in_flight_states
    }
    tree = ast.parse(source)
    constants = {
        target.id: _words(statement.value, {})
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "in_"
            and node.args
        ):
            model, checked = _model_state(node.func.value), node.args[0]
        elif (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
        ):
            model, checked = _model_state(node.left), node.comparators[0]
        else:
            continue
        if model in in_flight and in_flight[model] & _words(checked, constants):
            lines.append(node.lineno)
    return sorted(lines)
