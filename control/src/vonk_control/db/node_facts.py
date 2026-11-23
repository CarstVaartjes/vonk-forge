"""One-time transactional adoption of retired per-node fact owners."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

# The contact clock that dates each moved fact; a fact without one is kept
# when the old owner has none (columns re-added by a rolled-back Controller are
# NULL until that Controller writes them, so a value there is newer).
_CONTACT_CLOCKS = {
    "last_seen_at": ("last_seen_at",),
    "protocol_version": ("last_seen_at",),
    "contact_observation_digest": ("last_seen_at",),
    "contact_certificate_serial": ("last_seen_at", "management_observed_at"),
    "management_address": ("management_observed_at",),
    "management_observed_at": ("management_observed_at",),
    "recipe_observation_http_status": ("recipe_observation_status_at",),
    "recipe_observation_status_at": ("recipe_observation_status_at",),
}


def move_contact_facts(connection: Connection) -> None:
    """Give every node its contact row, adopting facts still on agent_nodes.

    Copy before dropping, in startup's one transaction: a crash rolls back
    both effects and the next start repeats the move. After an upgrade, a
    rollback and an upgrade again, each fact takes the newer value by its
    contact clock, so the rolled-back Controller's contacts are not lost.
    """
    from ..models.fleet import AgentNodeContact

    # Contact facts lived on agent_nodes until owner-approved lock-reduction
    # step 1 moved them; their names are the contact row's columns.
    old = {column["name"] for column in inspect(connection).get_columns("agent_nodes")}
    retired = sorted(
        old.intersection(AgentNodeContact.__table__.columns.keys()) - {"node_id"}
    )
    names = ", ".join(["node_id", *retired])

    def newer(name: str) -> str:
        clocks = [clock for clock in _CONTACT_CLOCKS.get(name, ()) if clock in old]
        if not clocks:
            return "true"
        return " OR ".join(
            f"(excluded.{clock} IS NOT NULL AND (agent_node_contacts.{clock} IS NULL"
            f" OR excluded.{clock} > agent_node_contacts.{clock}))"
            for clock in clocks
        )

    adopt = (
        "DO UPDATE SET "
        + ", ".join(
            f"{name} = CASE WHEN excluded.{name} IS NOT NULL AND ({newer(name)})"
            f" THEN excluded.{name}"
            f" ELSE agent_node_contacts.{name} END"
            for name in retired
        )
        if retired
        else "DO NOTHING"
    )
    connection.execute(
        text(
            f"INSERT INTO agent_node_contacts ({names}) "
            # WHERE true: SQLite parses ON CONFLICT after INSERT ... SELECT only so.
            f"SELECT {names} FROM agent_nodes WHERE true "
            f"ON CONFLICT (node_id) {adopt}"
        )
    )
    for name in retired:
        connection.execute(text(f"ALTER TABLE agent_nodes DROP COLUMN {name}"))


def move_runtime_facts(connection: Connection) -> None:
    """Adopt old facts once, then remove their columns in the same transaction."""
    import hashlib
    import uuid
    from datetime import UTC, datetime

    from pydantic import ValidationError
    from sqlalchemy import insert, select

    from vonk_agent_protocol.claims import AgentRuntimeIdentity
    from vonk_agent_protocol.contracts import AgentOperation, canonical_message
    from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
    from vonk_agent_protocol.runtime_preflight import RuntimePreflightResult

    from ..inventory.items_contract import (
        AgentInventoryItems,
        InventorySnapshotKind,
        PreflightInventoryItem,
    )
    from ..models.telemetry import NodeInventoryItemsSnapshot
    from .constants import _LOGGER

    inspector = inspect(connection)
    old_columns = {column["name"] for column in inspector.get_columns("agent_nodes")}
    if "architecture" not in old_columns:
        return
    # The replacement uniqueness rule exists before the previous report-only
    # rule is retired. Items and a report may have the same observation time.
    constraints = inspector.get_unique_constraints("node_inventory_snapshots")
    if any(item["name"] == "uq_inventory_node_observed" for item in constraints):
        connection.execute(
            text(
                "ALTER TABLE node_inventory_snapshots DROP CONSTRAINT uq_inventory_node_observed"
            )
        )
    now = datetime.now(UTC)
    results = (
        connection.execute(
            text("""
        SELECT DISTINCT ON (operation.node_id, operation.payload_digest)
            operation.node_id, operation.payload_digest, attempt.result, operation.updated_at
        FROM agent_operations AS operation
        JOIN agent_operation_attempts AS attempt
          ON attempt.operation_id = operation.id
         AND attempt.attempt = operation.current_attempt
        WHERE operation.kind = :kind AND operation.state = :state
        ORDER BY operation.node_id, operation.payload_digest,
                 operation.updated_at DESC, operation.id DESC
    """),
            {
                "kind": AgentOperation.RUNTIME_PREFLIGHT.value,
                "state": LifecycleState.SUCCEEDED.value,
            },
        )
        .mappings()
        .all()
    )
    preflights: dict[str, list[PreflightInventoryItem]] = {}
    for result in results:
        try:
            entry = PreflightInventoryItem(
                requirements_sha256=result["payload_digest"],
                received_at=result["updated_at"],
                result=RuntimePreflightResult.model_validate_json(
                    canonical_message(result["result"])
                ),
            )
        except ValidationError:
            # The corrupt receipt remains on its operation. It cannot be made
            # current inventory; normal preflight prepares fresh evidence.
            _LOGGER.warning("Unusable preflight receipt for node %s", result["node_id"])
            continue
        preflights.setdefault(result["node_id"], []).append(entry)
    awaiting_complete_claim = False
    rows = connection.execute(
        text("""
        SELECT node.node_id, node.architecture, node.semantic_version,
            node.build_digest, node.binary_digest, node.preflight_fingerprint,
            contact.last_seen_at, profile.hostname
        FROM agent_nodes AS node LEFT JOIN agent_node_profiles AS profile
          ON profile.node_id = node.node_id
        LEFT JOIN agent_node_contacts AS contact ON contact.node_id = node.node_id
    """)
    ).mappings()
    for row in rows:
        identity = None
        if all(
            row[field] is not None
            for field in AgentRuntimeIdentity.model_fields
            if field != "package_activation"
        ):
            identity = AgentRuntimeIdentity.model_validate_json(
                canonical_message(
                    {
                        field: row[field]
                        for field in AgentRuntimeIdentity.model_fields
                        if field != "package_activation"
                    }
                )
            )
        items = AgentInventoryItems(
            runtime_identity=identity,
            hostname=row["hostname"] or None,
            preflight_fingerprint=row["preflight_fingerprint"],
            preflights=preflights.get(row["node_id"], []),
        )
        digest = hashlib.sha256(
            canonical_message({"node_id": row["node_id"], "agent_items": items})
        ).hexdigest()
        existing = connection.scalar(
            select(NodeInventoryItemsSnapshot.id).where(
                NodeInventoryItemsSnapshot.node_id == row["node_id"]
            )
        )
        if existing is None:
            connection.execute(
                insert(NodeInventoryItemsSnapshot).values(
                    id=str(uuid.uuid4()),
                    node_id=row["node_id"],
                    kind=InventorySnapshotKind.ITEMS,
                    observed_at=row["last_seen_at"],
                    received_at=now,
                    evidence_digest=digest,
                    agent_items=items,
                )
            )
        identity_fields = set(AgentRuntimeIdentity.model_fields) - {
            "package_activation"
        }
        if identity is None and any(
            row[field] is not None for field in identity_fields
        ):
            current = connection.scalar(
                select(NodeInventoryItemsSnapshot.agent_items).where(
                    NodeInventoryItemsSnapshot.node_id == row["node_id"]
                )
            )
            if (
                not isinstance(current, AgentInventoryItems)
                or current.runtime_identity is None
            ):
                awaiting_complete_claim = True
                _LOGGER.warning(
                    "Retained incomplete runtime evidence for node %s in retired columns; "
                    "a fresh claim supplies the current identity, and the next startup retires the columns",
                    row["node_id"],
                )
        # This hook is atomic and runs before the new Controller accepts work;
        # if an item already exists after a committed adoption, never overwrite it.
    if awaiting_complete_claim:
        return
    for name in (
        "architecture",
        "semantic_version",
        "build_digest",
        "binary_digest",
        "preflight_fingerprint",
    ):
        connection.execute(text(f"ALTER TABLE agent_nodes DROP COLUMN {name}"))
    connection.execute(text("ALTER TABLE agent_node_profiles DROP COLUMN hostname"))


def move_resource_facts(connection: Connection) -> None:
    """Adopt each sample's measurements into inventory before retiring columns.

    Receipt identity and the source FK preserve historical observations and
    their retention lifetime. All writes and drops share startup's transaction.
    """
    import hashlib
    import uuid

    from sqlalchemy import insert, select

    from vonk_agent_protocol.contracts import canonical_message
    from vonk_agent_protocol.telemetry import HostResourceSample

    from ..inventory.items_contract import InventorySnapshotKind
    from ..models.telemetry import NodeInventoryResourceSnapshot

    inspector = inspect(connection)
    # Replacement report-only uniqueness was installed by reconciliation.
    for constraint in inspector.get_unique_constraints("node_inventory_snapshots"):
        if constraint["name"] == "uq_inventory_node_kind_observed":
            connection.execute(
                text(
                    "ALTER TABLE node_inventory_snapshots DROP CONSTRAINT uq_inventory_node_kind_observed"
                )
            )
    columns = {
        column["name"] for column in inspector.get_columns("node_telemetry_samples")
    }
    if "memory_total_bytes" not in columns:
        return
    # These rows are protected by the original pair/range constraints. Validate
    # the actual JSON contract too, so invalid input rolls back without loss.
    existing = set(
        connection.scalars(select(NodeInventoryResourceSnapshot.source_sample_id))
    )
    rows = connection.execute(
        text(
            "SELECT id, node_id, observed_at, received_at, memory_total_bytes, memory_available_bytes, disk_total_bytes, disk_free_bytes FROM node_telemetry_samples"
        )
    ).mappings()
    for row in rows:
        if row["id"] in existing:
            continue
        resources = HostResourceSample.model_validate_json(
            canonical_message(
                {name: row[name] for name in HostResourceSample.model_fields}
            )
        )
        connection.execute(
            insert(NodeInventoryResourceSnapshot).values(
                id=str(uuid.uuid4()),
                node_id=row["node_id"],
                kind=InventorySnapshotKind.MEASUREMENTS,
                observed_at=row["observed_at"],
                received_at=row["received_at"],
                source_sample_id=row["id"],
                host_resources=resources,
                evidence_digest=hashlib.sha256(
                    canonical_message(
                        {
                            "node_id": row["node_id"],
                            "source_sample_id": row["id"],
                            "host_resources": resources,
                        }
                    )
                ).hexdigest(),
            )
        )
    for name in HostResourceSample.model_fields:
        # Field identifiers come only from the canonical contract.
        connection.execute(
            text(f'ALTER TABLE node_telemetry_samples DROP COLUMN "{name}"')
        )
